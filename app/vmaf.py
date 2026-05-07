"""Post-encode VMAF measurement using ffmpeg's libvmaf filter.

Methodology mirrors the standalone deep-inspect service
(`vmaf-service/vmaf_runner.py`):

  - Both inputs decoded at native frame rate (NO `-r` override). Forcing
    a fixed framerate at input was the historical "match ab-av1's
    predictions" hack — it inflated scores on motion-heavy content
    relative to a full-clip honest measurement.
  - Both inputs are normalised through `setpts=PTS-STARTPTS,
    scale=W:H:flags=bicubic, format=yuv420p` so libvmaf sees matched
    timing, dimensions, and pixel format regardless of source bit depth.
  - Explicit `model=version=vmaf_v0.6.1` so we don't drift if a future
    libvmaf changes its default.

The numbers this returns now agree with the deep-inspect service to
within rounding. They will be lower than the value ab-av1 / av1an
predicted during target-quality probing — that gap is real (sample-based
predictors run on normalised chunks and are systematically optimistic),
which is why the backends now compensate by encoding to a slightly
higher internal target than the user picked.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
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
    """Run libvmaf and return {'vmaf': float, 'json_path': str, ...}.

    Aligns with Netflix's recommended methodology and the standalone
    deep-inspect service: native-fps decoding on both inputs, explicit
    yuv420p conversion, and the v0.6.1 1k model. Sub-1080p inputs are
    upscaled to 1920×1080 (the model's training resolution) to match
    ab-av1's `--vmaf-scale auto`.
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

    # Pick the scale target from the REFERENCE (the source of truth for
    # what dimensions matter); the distorted file may be a different
    # resolution if the encoder downscaled. libvmaf only requires both
    # inputs match each other.
    ref_dims = _video_dims(caps.ffprobe, reference_path) or (1920, 1080)
    width, height = ref_dims
    if width <= 2560 and height <= 1440:
        # 1k model territory. Upscale only if smaller than ab-av1's threshold.
        if width < 1728 and height < 972:
            target_w, target_h = 1920, 1080
        else:
            target_w, target_h = width, height
    else:
        # 4k tier; the 4k model expects 3840x2160 input
        target_w, target_h = (3840, 2160) if width < 3456 and height < 1944 else (width, height)

    # `setpts=PTS-STARTPTS` zeros each input's first-frame timestamp so
    # libvmaf's framesync pairs frame-0 with frame-0 even if a container
    # reports a non-zero start_time (common with mkv/mp4 from av1an or
    # ffmpeg's mux delay). `format=yuv420p` normalises bit depth — DJI
    # 10-bit HEVC vs 8-bit SVT-AV1 output would otherwise force libvmaf
    # to silently auto-convert one of them.
    sp = "setpts=PTS-STARTPTS"
    scale = f"scale={target_w}:{target_h}:flags=bicubic"
    chain = f"{sp},{scale},format=yuv420p"

    libvmaf_opts = (
        f"log_path={json_filename}"
        f":log_fmt=json"
        f":model=version=vmaf_v0.6.1"
        f":n_threads={threads}"
        f":pool=mean"
    )

    lavfi = (
        f"[0:v]{chain}[dist];"
        f"[1:v]{chain}[ref];"
        f"[dist][ref]libvmaf={libvmaf_opts}"
    )

    cmd = [
        caps.ffmpeg, "-hide_banner", "-nostats",
        "-i", distorted_path,
        "-i", reference_path,
        "-lavfi", lavfi,
        "-an", "-f", "null", "-",
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
        "scale_target": [target_w, target_h],
        "model": "vmaf_v0.6.1",
    }
