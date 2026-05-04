"""Post-encode VMAF measurement using ffmpeg's libvmaf filter."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from .progress import JobEvent, JobState
from .tools import get_capabilities


RE_TIME = re.compile(r"time=(\d+):(\d+):([\d.]+)")
RE_FRAME = re.compile(r"frame=\s*(\d+)")


def _video_dims(ffprobe: Optional[str], path: str) -> Optional[tuple[int, int]]:
    if not ffprobe:
        return None
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and "x" in r.stdout:
            w, h = r.stdout.strip().split("x")
            return int(w), int(h)
    except Exception:
        return None
    return None


async def measure_vmaf(reference_path: str, distorted_path: str, duration_s: float,
                        job: Optional[JobState] = None) -> dict:
    """Run libvmaf and return {'vmaf': float, 'json_path': str}.

    Aligns with the methodology Netflix recommends and ab-av1 uses by default:
    upscale sub-1080p inputs to 1080p (the 1k model's native resolution) and
    decode both inputs at a fixed 25 fps. Without this alignment the same file
    will score systematically lower than ab-av1's predictions.
    """
    caps = get_capabilities()
    if not caps.ffmpeg or not caps.libvmaf:
        raise RuntimeError("ffmpeg with libvmaf is required for VMAF measurement")

    threads = max(1, (os.cpu_count() or 4) - 1)

    out_dir = Path(distorted_path).parent
    json_filename = Path(distorted_path).stem + ".vmaf.json"
    json_path = out_dir / json_filename
    if json_path.exists():
        json_path.unlink()

    # Determine scaling target — match ab-av1's auto behaviour.
    width, height = _video_dims(caps.ffprobe, distorted_path) or (1920, 1080)
    if width <= 2560 and height <= 1440:
        # 1k model territory. Upscale only if smaller than ab-av1's threshold.
        if width < 1728 and height < 972:
            target_w, target_h = 1920, 1080
        else:
            target_w, target_h = width, height
    else:
        # 4k tier; the 4k model expects 3840x2160 input
        target_w, target_h = (3840, 2160) if width < 3456 and height < 1944 else (width, height)

    needs_scale = (target_w, target_h) != (width, height)
    scale_chain = f"scale={target_w}:{target_h}:flags=bicubic," if needs_scale else ""

    lavfi = (
        f"[0:v]{scale_chain}setpts=PTS-STARTPTS[dist];"
        f"[1:v]{scale_chain}setpts=PTS-STARTPTS[ref];"
        f"[dist][ref]libvmaf=log_path={json_filename}:log_fmt=json:n_threads={threads}"
    )

    cmd = [
        caps.ffmpeg, "-hide_banner", "-nostats",
        "-r", "25", "-i", distorted_path,
        "-r", "25", "-i", reference_path,
        "-lavfi", lavfi,
        "-f", "null", "-",
    ]

    if job:
        await job.emit(JobEvent(
            type="stage", stage="measuring", percent=93,
            message="Running libvmaf to compare encoded vs original…",
        ))

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(out_dir),
    )

    state = {"last_emit": 0.0}
    assert process.stdout is not None
    buf: list[str] = []
    while True:
        raw = await process.stdout.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        # ffmpeg also emits \r-progress lines
        for sub in line.split("\r"):
            sub = sub.strip()
            if not sub:
                continue
            buf.append(sub)
            if job:
                m = RE_TIME.search(sub)
                if m and duration_s > 0:
                    hh, mm, ss = int(m.group(1)), int(m.group(2)), float(m.group(3))
                    cur = hh * 3600 + mm * 60 + ss
                    ratio = max(0.0, min(1.0, cur / duration_s))
                    now = time.time()
                    if now - state["last_emit"] >= 0.3:
                        state["last_emit"] = now
                        await job.emit(JobEvent(
                            type="progress", stage="measuring",
                            percent=93 + ratio * 6,
                            message=f"VMAF analysis: {cur:.1f}s / {duration_s:.1f}s",
                        ))

    rc = await process.wait()
    if rc != 0:
        tail = "\n".join(buf[-20:])
        raise RuntimeError(f"libvmaf failed with code {rc}\n{tail}")

    if not json_path.exists():
        raise RuntimeError("libvmaf did not write its JSON output")

    try:
        with json_path.open("r") as f:
            data = json.load(f)
    except Exception as e:
        raise RuntimeError(f"failed to parse libvmaf JSON: {e}")

    pooled = data.get("pooled_metrics", {}).get("vmaf", {})
    score = pooled.get("mean")
    if score is None:
        # Some libvmaf versions store it differently
        frames = data.get("frames", [])
        if frames:
            scores = [f.get("metrics", {}).get("vmaf") for f in frames if f.get("metrics", {}).get("vmaf") is not None]
            if scores:
                score = sum(scores) / len(scores)
    if score is None:
        raise RuntimeError("Could not extract VMAF score from libvmaf output")

    return {
        "vmaf": float(score),
        "vmaf_min": pooled.get("min"),
        "vmaf_max": pooled.get("max"),
        "vmaf_harmonic_mean": pooled.get("harmonic_mean"),
        "json_path": str(json_path),
    }
