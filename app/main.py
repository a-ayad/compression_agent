"""FastAPI application: routes for upload, analyze, encode (SSE progress), download."""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

# Force UTF-8 console output on Windows so unicode characters in user-facing
# strings (and our pretty banner) don't crash when the terminal is cp1252.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Subprocess support on Windows requires the ProactorEventLoop. Some uvicorn
# auto-loop modes pick SelectorEventLoop which raises NotImplementedError on
# create_subprocess_exec.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, UploadFile, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .analysis import analyze, info_to_dict
from .encoders import list_encoders, get_encoder
from .progress import JobEvent, registry
from .tools import get_capabilities
from .vmaf import measure_vmaf
from .backends.ab_av1 import AbAv1Backend
from .backends.av1an import Av1anBackend
from .backends.base import EncodeRequest


ROOT = Path(__file__).resolve().parent.parent
UPLOAD_DIR = ROOT / "uploads"
OUTPUT_DIR = ROOT / "outputs"
STATIC_DIR = ROOT / "static"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# In-memory upload registry. For a real deployment swap for sqlite.
_uploads: dict[str, dict] = {}


def _get_backend(name: str):
    if name == "ab-av1":
        return AbAv1Backend()
    if name == "av1an":
        return Av1anBackend()
    raise ValueError(f"Unknown backend: {name}")


app = FastAPI(title="Video Compression Agent", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    caps = get_capabilities(refresh=True)
    bar = "-" * 60
    print(bar)
    print("Capabilities detected:")
    print(f"  ffmpeg     : {caps.ffmpeg}")
    print(f"  ffprobe    : {caps.ffprobe}")
    print(f"  ab-av1     : {caps.ab_av1}")
    print(f"  av1an      : {caps.av1an or '(not installed)'}")
    print(f"  vapoursynth: {caps.vapoursynth}")
    print(f"  libvmaf    : {caps.libvmaf}")
    print(f"  NVENC      : h264={caps.nvenc['h264']} hevc={caps.nvenc['hevc']} av1={caps.nvenc['av1']}")
    if caps.errors:
        print("  warnings :")
        for e in caps.errors:
            print(f"    - {e}")
    print(bar)


# ── Static index ───────────────────────────────────────────────────────────
@app.get("/")
async def index():
    idx = STATIC_DIR / "index.html"
    if not idx.exists():
        return JSONResponse({"error": "frontend not built"}, status_code=500)
    return FileResponse(idx)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Capabilities + catalog ─────────────────────────────────────────────────
@app.get("/api/capabilities")
async def api_capabilities():
    caps = get_capabilities()
    encoders = list_encoders()
    backends = []
    for backend_name in ("ab-av1", "av1an"):
        b = _get_backend(backend_name)
        ok, why = await b.is_available()
        backends.append({"name": backend_name, "available": ok, "reason": why})
    return {
        "capabilities": caps.to_dict(),
        "encoders": encoders,
        "backends": backends,
        "vmaf_targets": [
            {"value": 85, "label": "Good (VMAF 85)", "blurb": "Smaller file, casual viewing"},
            {"value": 90, "label": "Very Good (VMAF 90)", "blurb": "Balanced size/quality — recommended"},
            {"value": 95, "label": "Excellent (VMAF 95)", "blurb": "Visually transparent, larger file"},
        ],
    }


# ── Upload + analyze ───────────────────────────────────────────────────────
@app.post("/api/upload")
async def api_upload(file: UploadFile):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    upload_id = uuid.uuid4().hex[:12]
    safe_name = "".join(c for c in file.filename if c.isalnum() or c in "._- ").strip()
    if not safe_name:
        safe_name = f"upload_{upload_id}.bin"
    dest = UPLOAD_DIR / f"{upload_id}_{safe_name}"
    size = 0
    with dest.open("wb") as f:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            size += len(chunk)

    try:
        info = analyze(str(dest))
    except Exception as e:
        # Keep the file for debug but tell client what failed
        raise HTTPException(status_code=400, detail=f"Analysis failed: {e}")

    _uploads[upload_id] = {
        "id": upload_id,
        "path": str(dest),
        "filename": file.filename,
        "size": size,
        "info": info_to_dict(info),
    }
    return {"upload_id": upload_id, "info": info_to_dict(info)}


# ── Start encode ───────────────────────────────────────────────────────────
class EncodeBody(BaseModel):
    upload_id: str
    backend: str            # "ab-av1" | "av1an"
    encoder_id: str
    target_vmaf: float
    workers: int | None = None
    # Optional override of the encoder's default preset (SVT-AV1 0-13,
    # x264/x265 string presets aren't supported here yet). The backend
    # rewrites `--preset N` in the encoder params before launching.
    encoder_preset: int | None = None


@app.post("/api/encode")
async def api_encode(body: EncodeBody):
    upload = _uploads.get(body.upload_id)
    if not upload:
        raise HTTPException(status_code=404, detail="Unknown upload_id")
    try:
        encoder = get_encoder(body.encoder_id)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if body.backend not in encoder.backends:
        raise HTTPException(
            status_code=400,
            detail=f"{encoder.id} cannot run on backend {body.backend}",
        )

    job = registry.new_job()
    out_name = f"{Path(upload['path']).stem}_{encoder.id}_vmaf{int(body.target_vmaf)}.{encoder.container}"
    out_path = OUTPUT_DIR / out_name

    extra: dict = {}
    if body.workers:
        extra["workers"] = body.workers
    if body.encoder_preset is not None:
        extra["encoder_preset"] = body.encoder_preset
    req = EncodeRequest(
        input_path=upload["path"],
        output_path=str(out_path),
        encoder=encoder,
        target_vmaf=body.target_vmaf,
        extra_options=extra,
    )

    asyncio.create_task(_run_encode_job(req, body.backend, job, upload))
    return {"job_id": job.job_id}


async def _run_encode_job(req: EncodeRequest, backend_name: str, job, upload):
    """Drives backend encode + post-encode VMAF measurement."""
    started = time.time()
    try:
        await job.emit(JobEvent(
            type="stage", stage="queued", percent=0,
            message=f"Job queued — backend {backend_name}, encoder {req.encoder.id}, target VMAF {req.target_vmaf}",
        ))

        backend = _get_backend(backend_name)
        info = upload["info"]
        encode_result = await backend.encode(req, job)

        # Post-encode VMAF measurement
        try:
            vmaf_result = await measure_vmaf(
                reference_path=req.input_path,
                distorted_path=req.output_path,
                duration_s=info["duration_s"],
                job=job,
            )
        except Exception as e:
            await job.emit(JobEvent(
                type="log", stage="measuring",
                message=f"VMAF measurement failed: {e}",
            ))
            vmaf_result = {"vmaf": None, "error": str(e)}

        elapsed = time.time() - started
        out_path = Path(req.output_path)
        out_size = out_path.stat().st_size if out_path.exists() else 0
        in_size = info["size_bytes"]
        savings_pct = (1 - out_size / in_size) * 100 if in_size else 0.0

        result = {
            "input": {
                "url": f"/api/file/upload/{upload['id']}",
                "size": in_size,
                "info": info,
            },
            "output": {
                "url": f"/api/file/output/{out_path.name}",
                "size": out_size,
                "filename": out_path.name,
                "encoder_id": req.encoder.id,
                "encoder_label": req.encoder.label,
                "target_vmaf": req.target_vmaf,
                "achieved_vmaf": vmaf_result.get("vmaf"),
                "vmaf_min": vmaf_result.get("vmaf_min"),
                "vmaf_max": vmaf_result.get("vmaf_max"),
            },
            "savings_pct": round(savings_pct, 2),
            "size_ratio": round(out_size / in_size, 3) if in_size else None,
            "elapsed_s": round(elapsed, 1),
            "backend": backend_name,
            "encode_meta": encode_result,
        }
        await job.emit(JobEvent(
            type="stage", stage="done", percent=100,
            message=f"Done in {elapsed:.1f}s — {savings_pct:.1f}% smaller, achieved VMAF {vmaf_result.get('vmaf')}",
        ))
        await job.emit(JobEvent(type="done", stage="done", data=result))
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        # str(e) can be empty (e.g. NotImplementedError raised by asyncio's
        # SelectorEventLoop on Windows). Fall back to the class name so the
        # client sees something actionable.
        msg = str(e).strip() or f"{type(e).__name__} (no detail)"
        await job.emit(JobEvent(
            type="error", stage="error", message=msg,
            data={"traceback": tb, "exception_class": type(e).__name__},
        ))


# ── SSE progress stream ────────────────────────────────────────────────────
@app.get("/api/progress/{job_id}")
async def api_progress(job_id: str):
    state = registry.get(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="Unknown job_id")

    async def event_stream():
        listener, snapshot = state.subscribe()
        try:
            # Replay everything seen before subscription
            for ev in snapshot:
                yield ev.to_sse()
            if state.finished:
                return
            seen_ids = {id(ev) for ev in snapshot}
            while True:
                try:
                    ev = await asyncio.wait_for(listener.get(), timeout=15)
                    if id(ev) in seen_ids:
                        continue
                    yield ev.to_sse()
                    if ev.type in ("done", "error"):
                        return
                except asyncio.TimeoutError:
                    # heartbeat
                    yield ": ping\n\n"
        finally:
            state.unsubscribe(listener)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── File serving ───────────────────────────────────────────────────────────
@app.get("/api/file/upload/{upload_id}")
async def api_get_upload(upload_id: str):
    up = _uploads.get(upload_id)
    if not up:
        raise HTTPException(status_code=404, detail="Unknown upload")
    p = Path(up["path"])
    if not p.exists():
        raise HTTPException(status_code=404, detail="File missing")
    return FileResponse(p, media_type=_mime_for(p), filename=p.name)


@app.get("/api/file/output/{name}")
async def api_get_output(name: str):
    safe = Path(name).name
    p = OUTPUT_DIR / safe
    if not p.exists():
        raise HTTPException(status_code=404, detail="Output not found")
    return FileResponse(p, media_type=_mime_for(p), filename=p.name)


def _mime_for(p: Path) -> str:
    ext = p.suffix.lower()
    return {
        ".mp4": "video/mp4",
        ".mkv": "video/x-matroska",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".m4v": "video/x-m4v",
        ".avi": "video/x-msvideo",
    }.get(ext, "application/octet-stream")
