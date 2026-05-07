"""FastAPI app for the standalone CUDA-VMAF service.

Two interaction patterns:

1. **Browser** — user uploads files via the UI. Each upload is staged in
   $VMAF_WORK_DIR/uploads, registered in an in-memory dict, and shown in
   the file list so it can be re-used in subsequent comparisons without
   re-uploading.

2. **Programmatic** — POST {reference, distorted, ...} to /api/compare
   with absolute paths inside the container, or POST multipart to
   /api/upload to stage a file and pull its upload_id back.

The service does NOT share state with any other app. It owns its own
uploads/outputs volume; mounting the host dir into the container is
optional but lets the user drop files in via SSH/SMB instead of the UI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from vmaf_runner import (
    DEFAULT_MODEL,
    InspectResult,
    InspectScore,
    VMAFResult,
    cuda_available,
    gpu_present,
    run_inspect,
    run_vmaf,
)

WORK_DIR = Path(os.environ.get("VMAF_WORK_DIR", "/work"))
UPLOAD_DIR = WORK_DIR / "uploads"
WORK_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ── Logging ──────────────────────────────────────────────────────────────
# Send our app messages to a named logger so they're easy to grep for, and
# silence the /health access spam from uvicorn (the docker HEALTHCHECK and
# the vmaf-up.sh launcher both poll it every few seconds, drowning out the
# interesting lines like /api/compare).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vmaf")


class _DropHealthAccess(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "/health" not in msg and "/favicon.ico" not in msg


logging.getLogger("uvicorn.access").addFilter(_DropHealthAccess())


app = FastAPI(title="CUDA VMAF Service", version="1.0")
app.mount("/static", StaticFiles(directory="/app/static"), name="static")


# In-memory upload registry — same trade-off as the main compression app.
# Restarting the container clears upload IDs; the underlying files in
# UPLOAD_DIR survive and are picked up again at next startup so the user
# doesn't lose their work.
_uploads: dict[str, dict] = {}


def _bootstrap_registry() -> None:
    """Populate _uploads from disk so cross-restart files stay reachable."""
    for p in sorted(UPLOAD_DIR.iterdir(), key=lambda p: p.stat().st_mtime):
        if not p.is_file():
            continue
        # Filenames are stored as {upload_id}__{original_name}
        upload_id, _, original = p.name.partition("__")
        if not _ or not upload_id:
            # Pre-existing file without our naming scheme — synthesise an id
            upload_id = uuid.uuid4().hex[:12]
            original = p.name
        _uploads[upload_id] = {
            "id": upload_id,
            "path": str(p),
            "filename": original or p.name,
            "size": p.stat().st_size,
            "uploaded_at": p.stat().st_mtime,
        }


@app.on_event("startup")
async def _on_startup() -> None:
    _bootstrap_registry()
    log.info("─── CUDA VMAF Service starting ───")
    log.info("work_dir    : %s", WORK_DIR)
    log.info("upload_dir  : %s  (%d files registered)", UPLOAD_DIR, len(_uploads))
    log.info("cuda_filter : %s", cuda_available())
    log.info("gpu_present : %s", gpu_present())
    log.info("──────────────────────────────────")


# ─── Capabilities ────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "cuda_filter": cuda_available(),
        "gpu_present": gpu_present(),
        "work_dir": str(WORK_DIR),
    }


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(Path("/app/static/index.html").read_text())


# ─── Uploads ─────────────────────────────────────────────────────────────
class UploadOut(BaseModel):
    upload_id: str
    filename: str
    size: int
    uploaded_at: float


@app.post("/api/upload", response_model=UploadOut)
async def api_upload(file: UploadFile = File(...)) -> UploadOut:
    if not file.filename:
        raise HTTPException(400, "missing filename")
    upload_id = uuid.uuid4().hex[:12]
    safe = "".join(c for c in file.filename if c.isalnum() or c in "._- ").strip()
    safe = safe or f"upload_{upload_id}.bin"
    dest = UPLOAD_DIR / f"{upload_id}__{safe}"
    log.info("upload start: id=%s name=%r", upload_id, file.filename)
    t0 = time.monotonic()
    size = 0
    with dest.open("wb") as out:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            size += len(chunk)
    rec = {
        "id": upload_id, "path": str(dest), "filename": file.filename,
        "size": size, "uploaded_at": time.time(),
    }
    _uploads[upload_id] = rec
    log.info(
        "upload done : id=%s size=%s in %.2fs (%d files registered)",
        upload_id, _human_bytes(size), time.monotonic() - t0, len(_uploads),
    )
    return UploadOut(
        upload_id=upload_id, filename=file.filename, size=size,
        uploaded_at=rec["uploaded_at"],
    )


@app.delete("/api/upload/{upload_id}")
async def api_delete_upload(upload_id: str) -> dict:
    rec = _uploads.pop(upload_id, None)
    if not rec:
        log.warning("delete miss: id=%s (not registered)", upload_id)
        raise HTTPException(404, "unknown upload_id")
    try:
        Path(rec["path"]).unlink(missing_ok=True)
    except Exception as e:
        log.warning("delete file error: id=%s err=%s", upload_id, e)
    log.info("delete ok  : id=%s name=%r", upload_id, rec["filename"])
    return {"ok": True}


def _human_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n); i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024; i += 1
    return f"{f:.1f} {units[i]}" if i else f"{int(f)} {units[i]}"


@app.get("/api/files")
async def api_files() -> dict:
    items = []
    for rec in sorted(_uploads.values(), key=lambda r: r["uploaded_at"], reverse=True):
        if Path(rec["path"]).exists():
            items.append({
                "upload_id": rec["id"],
                "filename": rec["filename"],
                "size": rec["size"],
                "uploaded_at": rec["uploaded_at"],
            })
    return {"files": items}


# ─── Compare ─────────────────────────────────────────────────────────────
class CompareItem(BaseModel):
    """Reference to a file. Either upload_id (preferred) or path."""
    upload_id: Optional[str] = None
    path: Optional[str] = None


class CompareRequest(BaseModel):
    reference: CompareItem
    distorted: list[CompareItem] = Field(..., min_length=1, max_length=2)
    use_cuda: Optional[bool] = None
    model: str = DEFAULT_MODEL
    upscale_1080p: bool = False    # upscale sub-1080p inputs to 1080p


class ScoreOut(BaseModel):
    label: str
    upload_id: Optional[str] = None
    mean: float
    min: float
    max: float
    harmonic_mean: float
    frames: int          # frames libvmaf actually compared
    ref_frames: int      # frames in the reference (probed)
    dist_frames: int     # frames in the distorted (probed)
    frame_delta: int     # dist_frames - ref_frames; 0 = aligned
    target_w: int        # resolution actually fed to libvmaf
    target_h: int
    upscaled: bool       # True iff 1080p upscale option kicked in
    seconds: float


class CompareResponse(BaseModel):
    used_cuda: bool
    model: str
    reference_label: str
    scores: list[ScoreOut]


def _resolve(item: CompareItem) -> tuple[str, str, Optional[str]]:
    """Return (absolute_path, label, upload_id)."""
    if item.upload_id:
        rec = _uploads.get(item.upload_id)
        if not rec or not Path(rec["path"]).exists():
            raise HTTPException(404, f"unknown upload_id: {item.upload_id}")
        return rec["path"], rec["filename"], rec["id"]
    if item.path:
        p = Path(item.path)
        if not p.is_file():
            raise HTTPException(404, f"file not found: {item.path}")
        return str(p), p.name, None
    raise HTTPException(400, "compare item needs upload_id or path")


def _to_score(r: VMAFResult, label: str, upload_id: Optional[str]) -> ScoreOut:
    return ScoreOut(
        label=label, upload_id=upload_id,
        mean=round(r.mean, 4), min=round(r.min, 4), max=round(r.max, 4),
        harmonic_mean=round(r.harmonic_mean, 4),
        frames=r.frames,
        ref_frames=r.ref_frames, dist_frames=r.dist_frames,
        frame_delta=(r.dist_frames - r.ref_frames),
        target_w=r.target_w, target_h=r.target_h, upscaled=r.upscaled,
        seconds=round(r.seconds, 3),
    )


@app.post("/api/compare", response_model=CompareResponse)
async def api_compare(req: CompareRequest) -> CompareResponse:
    job = uuid.uuid4().hex[:6]
    ref_path, ref_label, _ = _resolve(req.reference)
    dist_specs = [(_resolve(d)) for d in req.distorted]

    requested = (
        "auto" if req.use_cuda is None else ("cuda" if req.use_cuda else "cpu")
    )
    log.info(
        "[%s] compare start: ref=%r vs %d distorted | mode=%s model=%s",
        job, ref_label, len(dist_specs), requested, req.model,
    )
    for i, (_, label, _uid) in enumerate(dist_specs):
        log.info("[%s]   distorted[%d] = %r", job, i, label)

    job_t0 = time.monotonic()
    try:
        results: list[tuple[VMAFResult, str, Optional[str]]] = []
        for idx, (path, label, upload_id) in enumerate(dist_specs):
            log.info("[%s] running vmaf %d/%d ...", job, idx + 1, len(dist_specs))
            r = await run_vmaf(
                reference=ref_path, distorted=path,
                use_cuda=req.use_cuda, model=req.model,
                upscale_1080p=req.upscale_1080p,
                log_prefix=f"{job}#{idx + 1}",
            )
            delta = r.dist_frames - r.ref_frames
            warn = f" ⚠ Δ={delta:+d}" if delta else ""
            up = " 1080p↑" if r.upscaled else ""
            log.info(
                "[%s]   ↳ %s: vmaf mean=%.2f min=%.2f max=%.2f frames=%d "
                "(ref=%s dist=%s%s) target=%dx%d%s in %.2fs (%s)",
                job, label, r.mean, r.min, r.max, r.frames,
                r.ref_frames or "?", r.dist_frames or "?", warn,
                r.target_w, r.target_h, up,
                r.seconds, "CUDA" if r.used_cuda else "CPU",
            )
            results.append((r, label, upload_id))
    except FileNotFoundError as e:
        log.warning("[%s] compare 404: %s", job, e)
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        log.error("[%s] compare 500: %s", job, e)
        raise HTTPException(500, str(e))

    log.info(
        "[%s] compare done in %.2fs total",
        job, time.monotonic() - job_t0,
    )
    return CompareResponse(
        used_cuda=results[0][0].used_cuda if results else False,
        model=req.model,
        reference_label=ref_label,
        scores=[_to_score(r, lbl, uid) for r, lbl, uid in results],
    )


# ─── Deep inspect (multi-metric + findings) ─────────────────────────────
class InspectRequest(BaseModel):
    reference: CompareItem
    # 1 distorted = single-clip inspection. 2 distorted = side-by-side
    # inspection of two encodes with cross-comparison findings.
    distorted: list[CompareItem] = Field(..., min_length=1, max_length=2)
    use_cuda: Optional[bool] = None
    upscale_1080p: bool = False


class Finding(BaseModel):
    severity: str    # "good" | "info" | "warn" | "alert" | "summary"
    title: str
    detail: str


class InspectScoreOut(BaseModel):
    label: str
    upload_id: Optional[str] = None

    dist_codec: str
    dist_bitrate_kbps: float
    bitrate_ratio: float
    dist_w: int
    dist_h: int
    dist_frames: int
    frame_delta: int

    vmaf: float
    vmaf_min: float
    vmaf_neg: float
    vmaf_neg_min: float
    enhancement_gap: float
    psnr_y_db: float
    ssim_y: float

    lap_var_dist: float
    detail_retention: float

    seconds: float
    dist_preview_b64: str

    findings: list[Finding]


class InspectResponse(BaseModel):
    reference_label: str
    used_cuda: bool

    # Reference-side info (shared across all scores)
    ref_codec: str
    ref_bitrate_kbps: float
    ref_w: int
    ref_h: int
    ref_frames: int
    target_w: int
    target_h: int
    upscaled: bool
    lap_var_ref: float
    ref_preview_b64: str

    seconds: float
    scores: list[InspectScoreOut]
    pair_findings: list[Finding]    # only populated when len(scores) == 2


def _findings_for_score(s: InspectScore) -> list[Finding]:
    """Rule-based explanations of what the metric mix is telling us."""
    out: list[Finding] = []
    gap = s.enhancement_gap
    psnr = s.psnr_y_db
    ret_pct = s.detail_retention * 100.0
    br_ratio = s.bitrate_ratio

    # 1) Enhancement signature (regular VMAF vs neg model)
    if gap >= 4.0:
        out.append(Finding(
            severity="alert",
            title="VMAF score may be inflated by enhancement",
            detail=(
                f"Regular VMAF is {gap:+.1f} points higher than the neg "
                f"model ({s.vmaf_mean:.2f} vs {s.vmaf_neg_mean:.2f}). A gap "
                "≥ 4 points strongly suggests the encode applied sharpening, "
                "contrast/saturation tweaks, or VMAF-aware preprocessing — "
                "tricks that boost regular VMAF without improving fidelity. "
                f"Trust the neg score ({s.vmaf_neg_mean:.1f}) for this one."
            ),
        ))
    elif gap >= 2.0:
        out.append(Finding(
            severity="warn",
            title="Mild enhancement signature",
            detail=(
                f"Regular vs neg gap of {gap:+.1f} points — some "
                "preprocessing or encoder tuning is at play, but the "
                "effect is small."
            ),
        ))
    else:
        out.append(Finding(
            severity="good",
            title="No enhancement signature",
            detail=(
                f"Regular and neg VMAF agree within {abs(gap):.1f} points — "
                "the score is honest."
            ),
        ))

    # 2) Pixel fidelity (PSNR)
    if psnr >= 42.0:
        out.append(Finding(
            severity="good",
            title="Excellent pixel fidelity",
            detail=(
                f"PSNR(Y) of {psnr:.1f} dB indicates very small pixel-level "
                "differences from the reference."
            ),
        ))
    elif psnr >= 38.0:
        out.append(Finding(
            severity="info",
            title="Solid pixel fidelity",
            detail=(
                f"PSNR(Y) of {psnr:.1f} dB — typical for medium-bitrate "
                "streaming encodes; small per-pixel errors likely "
                "imperceptible at normal viewing distance."
            ),
        ))
    elif psnr >= 33.0:
        out.append(Finding(
            severity="warn",
            title="Noticeable pixel error",
            detail=(
                f"PSNR(Y) of {psnr:.1f} dB. Per-pixel differences are "
                "meaningful — looks acceptable but is materially different "
                "from the source."
            ),
        ))
    else:
        out.append(Finding(
            severity="alert",
            title="Significant pixel error",
            detail=(
                f"PSNR(Y) of {psnr:.1f} dB. The encoded clip is far from "
                "the source at the pixel level; expect visible quality loss."
            ),
        ))

    # 3) Detail retention (Laplacian variance ratio)
    if ret_pct >= 95.0:
        out.append(Finding(
            severity="good",
            title="Detail well preserved",
            detail=(
                f"~{ret_pct:.0f}% of the reference's high-frequency content "
                "retained. Fine textures (skin, hair, fabric, grain) are intact."
            ),
        ))
    elif ret_pct >= 85.0:
        out.append(Finding(
            severity="info",
            title="Light smoothing",
            detail=(
                f"~{100 - ret_pct:.0f}% of fine detail dropped. Subtle "
                "textures have been softened — common in modern codecs at "
                "moderate bitrates."
            ),
        ))
    elif ret_pct >= 70.0:
        out.append(Finding(
            severity="warn",
            title="Noticeable detail removal",
            detail=(
                f"~{100 - ret_pct:.0f}% of fine detail discarded. "
                "Skin texture, fabric grain, hair strands likely visibly "
                "smoothed."
            ),
        ))
    else:
        out.append(Finding(
            severity="alert",
            title="Heavy detail removal / denoising",
            detail=(
                f"Only {ret_pct:.0f}% of high-frequency content retained. "
                "Aggressive denoising or smoothing — fine texture is gone."
            ),
        ))

    # 4) SSIM corroboration
    if s.ssim_y >= 0.99 and psnr < 38.0:
        out.append(Finding(
            severity="info",
            title="Structure preserved, pixels diverged",
            detail=(
                f"SSIM(Y) is {s.ssim_y:.4f} (near-identical structure) "
                f"but PSNR is only {psnr:.1f} dB. Means the encode preserves "
                "edges, shapes and overall layout but has shifted pixel "
                "values — typical of denoising/smoothing rather than "
                "structural compression artifacts."
            ),
        ))

    # 5) Bitrate regime context
    if br_ratio and br_ratio < 0.05:
        ratio_x = (1.0 / br_ratio) if br_ratio > 0 else 0
        out.append(Finding(
            severity="info",
            title=f"Extreme compression ({ratio_x:.0f}× smaller than reference)",
            detail=(
                f"Distorted is {br_ratio * 100:.1f}% of the reference's "
                "bitrate. At this ratio, perceived quality is achieved by "
                "deciding what to discard rather than by faithful reproduction."
            ),
        ))

    # 6) Frame-count mismatch (already shown elsewhere — restate)
    if s.frame_delta:
        out.append(Finding(
            severity="warn",
            title=f"Frame-count mismatch (Δ={s.frame_delta:+d})",
            detail=(
                f"dist={s.dist_frames}, see ref above. libvmaf trims "
                "to the shorter stream; if the extra frame is at the start, "
                "every per-frame score is shifted by one. setpts=PTS-STARTPTS "
                "is applied to align the t=0 frame."
            ),
        ))

    # 7) Composite verdict
    if gap >= 4.0 and (psnr < 38.0 or ret_pct < 85.0):
        out.append(Finding(
            severity="summary",
            title="Verdict: VMAF-tuned delivery encode",
            detail=(
                "This file trades pixel fidelity and/or fine detail for a "
                "high VMAF score. Good fit for bandwidth-constrained "
                "delivery; not appropriate for archival or further "
                "re-encoding. Use the neg score for honest comparisons."
            ),
        ))
    elif gap < 2.0 and psnr >= 40.0 and ret_pct >= 90.0:
        out.append(Finding(
            severity="summary",
            title="Verdict: Honest high-quality encode",
            detail=(
                "Quality metrics agree across the board — VMAF, neg, "
                "PSNR, SSIM and detail retention are all consistent. "
                "Score reflects real fidelity."
            ),
        ))
    elif gap < 2.0 and psnr < 35.0:
        out.append(Finding(
            severity="summary",
            title="Verdict: Honest low-bitrate encode",
            detail=(
                "Metrics agree the file is far from the source, but at "
                "least the score isn't being inflated by tricks. "
                "Genuinely a low-quality version, not a deceptive one."
            ),
        ))
    return out


@app.post("/api/inspect", response_model=InspectResponse)
async def api_inspect(req: InspectRequest) -> InspectResponse:
    job = uuid.uuid4().hex[:6]
    ref_path, ref_label, _ = _resolve(req.reference)
    dist_specs = [_resolve(d) for d in req.distorted]

    requested = (
        "auto" if req.use_cuda is None else ("cuda" if req.use_cuda else "cpu")
    )
    log.info(
        "[%s] inspect start: ref=%r vs %d distorted | mode=%s upscale=%s",
        job, ref_label, len(dist_specs), requested, req.upscale_1080p,
    )
    for i, (_, lbl, _uid) in enumerate(dist_specs):
        log.info("[%s]   distorted[%d] = %r", job, i, lbl)

    try:
        r = await run_inspect(
            reference=ref_path,
            distorted=[p for p, _, _ in dist_specs],
            use_cuda=req.use_cuda, upscale_1080p=req.upscale_1080p,
            log_prefix=job,
        )
    except FileNotFoundError as e:
        log.warning("[%s] inspect 404: %s", job, e)
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        log.error("[%s] inspect 500: %s", job, e)
        raise HTTPException(500, str(e))

    score_outs: list[InspectScoreOut] = []
    for s, (_, lbl, uid) in zip(r.scores, dist_specs):
        findings = _findings_for_score(s)
        score_outs.append(InspectScoreOut(
            label=lbl, upload_id=uid,
            dist_codec=s.dist_codec,
            dist_bitrate_kbps=round(s.dist_bitrate_kbps, 2),
            bitrate_ratio=round(s.bitrate_ratio, 4),
            dist_w=s.dist_w, dist_h=s.dist_h, dist_frames=s.dist_frames,
            frame_delta=s.frame_delta,
            vmaf=round(s.vmaf_mean, 4), vmaf_min=round(s.vmaf_min, 4),
            vmaf_neg=round(s.vmaf_neg_mean, 4),
            vmaf_neg_min=round(s.vmaf_neg_min, 4),
            enhancement_gap=round(s.enhancement_gap, 4),
            psnr_y_db=round(s.psnr_y_db, 4),
            ssim_y=round(s.ssim_y, 6),
            lap_var_dist=round(s.lap_var_dist, 2),
            detail_retention=round(s.detail_retention, 4),
            seconds=round(s.seconds, 3),
            dist_preview_b64=s.dist_preview_b64,
            findings=findings,
        ))

    pair_findings: list[Finding] = []
    if len(r.scores) == 2:
        pair_findings = _pair_findings(r.scores[0], r.scores[1],
                                       score_outs[0].label, score_outs[1].label)

    log.info("[%s] inspect → %d scores, %d pair findings, %.2fs",
             job, len(score_outs), len(pair_findings), r.seconds)

    return InspectResponse(
        reference_label=ref_label,
        used_cuda=r.used_cuda,
        ref_codec=r.ref_codec,
        ref_bitrate_kbps=round(r.ref_bitrate_kbps, 2),
        ref_w=r.ref_w, ref_h=r.ref_h, ref_frames=r.ref_frames,
        target_w=r.target_w, target_h=r.target_h, upscaled=r.upscaled,
        lap_var_ref=round(r.lap_var_ref, 2),
        ref_preview_b64=r.ref_preview_b64,
        seconds=round(r.seconds, 3),
        scores=score_outs,
        pair_findings=pair_findings,
    )


def _pair_findings(
    a: InspectScore, b: InspectScore,
    label_a: str, label_b: str,
) -> list[Finding]:
    """Cross-compare A vs B and explain what the differences mean.

    Conventions: A is whichever was picked first in the UI.
    """
    out: list[Finding] = []
    a_short = (label_a[:24] + "…") if len(label_a) > 27 else label_a
    b_short = (label_b[:24] + "…") if len(label_b) > 27 else label_b

    # 1. VMAF disagreement vs neg
    vmaf_lead = a.vmaf_mean - b.vmaf_mean
    neg_lead = a.vmaf_neg_mean - b.vmaf_neg_mean
    flipped = (vmaf_lead > 0) != (neg_lead > 0) and abs(vmaf_lead) > 0.5 and abs(neg_lead) > 0.5
    if flipped:
        winner_reg = a_short if vmaf_lead > 0 else b_short
        winner_neg = a_short if neg_lead > 0 else b_short
        out.append(Finding(
            severity="alert",
            title="Regular and neg VMAF disagree on which is better",
            detail=(
                f"Regular VMAF says {winner_reg} wins (Δ={vmaf_lead:+.2f}), "
                f"but the neg model picks {winner_neg} (Δ={neg_lead:+.2f}). "
                "When the two models flip rankings, one of the encodes is "
                "almost certainly using contrast/sharpness tricks that the "
                "neg model corrects for. Trust the neg ranking."
            ),
        ))
    elif abs(vmaf_lead - neg_lead) >= 2.0:
        bigger_gap = a_short if a.enhancement_gap > b.enhancement_gap else b_short
        out.append(Finding(
            severity="warn",
            title="One encode is more 'enhanced' than the other",
            detail=(
                f"{bigger_gap} has a larger regular-vs-neg gap "
                f"({max(a.enhancement_gap, b.enhancement_gap):+.2f} vs "
                f"{min(a.enhancement_gap, b.enhancement_gap):+.2f}). The "
                "encodes use different preprocessing — the one with the "
                "bigger gap relies more on perceptual tricks for its score."
            ),
        ))

    # 2. PSNR vs VMAF disagreement (the smoking gun pattern)
    psnr_lead = a.psnr_y_db - b.psnr_y_db
    if abs(vmaf_lead) >= 1.0 and abs(psnr_lead) >= 0.5 and (vmaf_lead > 0) != (psnr_lead > 0):
        vmaf_winner = a_short if vmaf_lead > 0 else b_short
        psnr_winner = a_short if psnr_lead > 0 else b_short
        out.append(Finding(
            severity="alert",
            title="VMAF and PSNR rank the encodes oppositely",
            detail=(
                f"VMAF prefers {vmaf_winner} (Δ={vmaf_lead:+.2f}) but PSNR "
                f"prefers {psnr_winner} (Δ={psnr_lead:+.2f} dB). PSNR is a "
                "pure pixel-distance metric — when it disagrees with VMAF, "
                "the higher-VMAF clip is hiding pixel error behind "
                "perceptually-clever processing."
            ),
        ))

    # 3. Bitrate efficiency at similar quality
    smaller, larger = (a, b) if a.dist_bitrate_kbps < b.dist_bitrate_kbps else (b, a)
    sm_label = a_short if smaller is a else b_short
    lg_label = a_short if larger is a else b_short
    if larger.dist_bitrate_kbps > 0:
        ratio = smaller.dist_bitrate_kbps / larger.dist_bitrate_kbps
        # Use the neg model for honest quality comparison
        q_gap_neg = smaller.vmaf_neg_mean - larger.vmaf_neg_mean
        if ratio < 0.5:
            if abs(q_gap_neg) <= 1.0:
                out.append(Finding(
                    severity="info",
                    title=f"{sm_label} achieves the same quality at {ratio*100:.0f}% the bitrate",
                    detail=(
                        f"{sm_label} ({smaller.dist_bitrate_kbps:.0f} kbps) "
                        f"and {lg_label} ({larger.dist_bitrate_kbps:.0f} kbps) "
                        f"have neg-VMAF within {abs(q_gap_neg):.1f} points of "
                        "each other. The smaller encode is genuinely more "
                        "bitrate-efficient at this operating point."
                    ),
                ))
            elif q_gap_neg < -2.0:
                out.append(Finding(
                    severity="info",
                    title=f"{sm_label} saves bitrate at the cost of quality",
                    detail=(
                        f"{sm_label} is {ratio*100:.0f}% the bitrate of "
                        f"{lg_label} but neg-VMAF is {q_gap_neg:.1f} points "
                        "lower — a real quality trade rather than a free lunch."
                    ),
                ))

    # 4. Detail retention difference
    a_ret = a.detail_retention * 100
    b_ret = b.detail_retention * 100
    if abs(a_ret - b_ret) >= 8.0:
        more, less = (a_short, b_short) if a_ret > b_ret else (b_short, a_short)
        more_pct = max(a_ret, b_ret)
        less_pct = min(a_ret, b_ret)
        out.append(Finding(
            severity="info",
            title=f"{more} preserves more fine detail than {less}",
            detail=(
                f"Detail retention: {more_pct:.0f}% vs {less_pct:.0f}%. "
                f"{less} has applied more denoising/smoothing — fine textures "
                f"like skin pores, hair strands and fabric grain are softer."
            ),
        ))

    # 5. Composite recommendation
    if (a.psnr_y_db >= 38 and b.psnr_y_db >= 38
            and a.enhancement_gap < 2.5 and b.enhancement_gap < 2.5):
        winner = a_short if a.vmaf_neg_mean > b.vmaf_neg_mean else b_short
        out.append(Finding(
            severity="summary",
            title=f"Recommendation: {winner} (both honest, picking by neg-VMAF)",
            detail=(
                "Both encodes are honest — no enhancement-gaming detected. "
                "Pick by raw quality (neg-VMAF) and bitrate that fits your "
                "delivery target."
            ),
        ))
    elif a.enhancement_gap >= 4.0 or b.enhancement_gap >= 4.0:
        cleaner = a_short if a.enhancement_gap < b.enhancement_gap else b_short
        cheater = b_short if cleaner == a_short else a_short
        out.append(Finding(
            severity="summary",
            title=f"Recommendation: {cleaner}",
            detail=(
                f"{cheater} is using enhancement to inflate its VMAF score. "
                f"For honest comparisons (and downstream re-encoding), prefer "
                f"{cleaner}."
            ),
        ))

    return out


# ─── Legacy upload-then-compare in one shot (kept for scripted use) ──────
@app.post("/vmaf-upload", response_model=CompareResponse)
async def api_vmaf_upload(
    reference: UploadFile = File(...),
    distorted_a: UploadFile = File(...),
    distorted_b: Optional[UploadFile] = File(None),
    use_cuda: Optional[bool] = Form(None),
    model: str = Form(DEFAULT_MODEL),
) -> CompareResponse:
    job_id = uuid.uuid4().hex[:8]
    job_dir = WORK_DIR / f"oneshot_{job_id}"
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        ref_p = job_dir / f"ref_{Path(reference.filename or 'ref.mp4').name}"
        await _save_upload(reference, ref_p)
        dists = [job_dir / f"distA_{Path(distorted_a.filename or 'A.mp4').name}"]
        await _save_upload(distorted_a, dists[0])
        if distorted_b is not None:
            dp = job_dir / f"distB_{Path(distorted_b.filename or 'B.mp4').name}"
            await _save_upload(distorted_b, dp)
            dists.append(dp)

        results: list[VMAFResult] = []
        for d in dists:
            r = await run_vmaf(
                reference=str(ref_p), distorted=str(d),
                use_cuda=use_cuda, model=model,
            )
            results.append(r)
        return CompareResponse(
            used_cuda=results[0].used_cuda if results else False,
            model=model,
            reference_label=reference.filename or ref_p.name,
            scores=[_to_score(r, Path(r.distorted).name, None) for r in results],
        )
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def _save_upload(f: UploadFile, dest: Path) -> None:
    with dest.open("wb") as out:
        while True:
            chunk = await f.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)


# ─── File serving (for previewing uploaded clips in the UI) ──────────────
@app.get("/api/file/{upload_id}")
async def api_file(upload_id: str) -> FileResponse:
    rec = _uploads.get(upload_id)
    if not rec:
        raise HTTPException(404, "unknown upload")
    p = Path(rec["path"])
    if not p.exists():
        raise HTTPException(404, "file missing on disk")
    return FileResponse(p, media_type=_mime(p), filename=rec["filename"])


def _mime(p: Path) -> str:
    return {
        ".mp4": "video/mp4", ".mkv": "video/x-matroska",
        ".webm": "video/webm", ".mov": "video/quicktime",
        ".m4v": "video/x-m4v", ".avi": "video/x-msvideo",
    }.get(p.suffix.lower(), "application/octet-stream")
