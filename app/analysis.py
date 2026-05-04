"""Video analysis: ffprobe metadata + compressibility scoring + recommendation."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from .tools import get_capabilities

# Source codec efficiency tiers — newer codecs at the same bpp produce better
# quality, so already-efficient sources have less recompression headroom.
EFFICIENCY = {
    "rawvideo": "very_inefficient", "mjpeg": "very_inefficient", "prores": "very_inefficient",
    "dvvideo": "very_inefficient", "mpeg2video": "very_inefficient", "mpeg4": "very_inefficient",
    "msmpeg4v2": "very_inefficient", "msmpeg4v3": "very_inefficient", "wmv2": "very_inefficient",
    "h263": "inefficient", "vp8": "inefficient", "mpeg1video": "inefficient",
    "h264": "moderate",
    "hevc": "efficient", "vp9": "efficient",
    "av1": "very_efficient",
}

EFFICIENCY_LABEL = {
    "very_inefficient": "very inefficient",
    "inefficient": "inefficient",
    "moderate": "moderately efficient",
    "efficient": "efficient",
    "very_efficient": "very efficient",
}


@dataclass
class VideoInfo:
    path: str
    filename: str
    size_bytes: int
    container: str
    duration_s: float
    codec: str
    codec_label: str
    width: int
    height: int
    fps: float
    bitrate_kbps: float
    pix_fmt: str
    bpp: float                # bits per pixel per frame
    compressibility: str      # "highly_compressible" | "compressible" | "moderate" | "already_efficient"
    compressibility_label: str
    verdict: str              # human-readable sentence
    recommendation: dict      # {"codec": "av1", "encoder_id": "...", "reasoning": "..."}


def _run_ffprobe(ffprobe: str, path: str) -> dict:
    cmd = [
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


def _parse_fps(rate_str: str) -> float:
    if not rate_str or rate_str == "0/0":
        return 0.0
    if "/" in rate_str:
        n, d = rate_str.split("/")
        try:
            n_f = float(n)
            d_f = float(d)
            return n_f / d_f if d_f else 0.0
        except ValueError:
            return 0.0
    try:
        return float(rate_str)
    except ValueError:
        return 0.0


def _classify_compressibility(bpp: float, source_efficiency: str) -> tuple[str, str]:
    # Adjust bpp threshold based on source codec maturity.
    # Modern codecs need lower bpp to match older codec quality.
    # An h264 at 0.08 bpp ≈ av1 at 0.04 bpp visually.
    if source_efficiency in ("very_efficient", "efficient"):
        if bpp >= 0.18:
            return "highly_compressible", "highly compressible"
        if bpp >= 0.10:
            return "compressible", "compressible"
        if bpp >= 0.05:
            return "moderate", "moderately compressed"
        return "already_efficient", "already well compressed"
    elif source_efficiency == "moderate":
        if bpp >= 0.20:
            return "highly_compressible", "highly compressible"
        if bpp >= 0.12:
            return "compressible", "compressible"
        if bpp >= 0.06:
            return "moderate", "moderately compressed"
        return "already_efficient", "already well compressed"
    else:  # inefficient sources almost always have headroom
        if bpp >= 0.30:
            return "highly_compressible", "highly compressible"
        if bpp >= 0.15:
            return "compressible", "compressible"
        return "moderate", "moderately compressed"


def _build_verdict(info_kwargs: dict, eff_label: str, comp_label: str, savings: int) -> str:
    codec = info_kwargs["codec_label"]
    res = f"{info_kwargs['width']}x{info_kwargs['height']}"
    br = info_kwargs["bitrate_kbps"]
    return (
        f"This is a {res} {codec} file at {br:,.0f} kbps "
        f"({info_kwargs['bpp']:.3f} bits/pixel/frame). The source codec is {eff_label} "
        f"and the file looks {comp_label} — expect roughly {savings}% size reduction "
        f"at high VMAF targets with a modern codec."
    )


def _expected_savings(comp: str, source_eff: str) -> int:
    table = {
        "highly_compressible":  {"very_inefficient": 80, "inefficient": 70, "moderate": 55, "efficient": 35, "very_efficient": 20},
        "compressible":         {"very_inefficient": 70, "inefficient": 60, "moderate": 40, "efficient": 25, "very_efficient": 12},
        "moderate":             {"very_inefficient": 55, "inefficient": 45, "moderate": 25, "efficient": 12, "very_efficient": 5},
        "already_efficient":    {"very_inefficient": 40, "inefficient": 30, "moderate": 10, "efficient": 5,  "very_efficient": 2},
    }
    return table.get(comp, {}).get(source_eff, 25)


def _recommend(codec: str, source_eff: str, comp: str, has_av1_nvenc: bool, has_hevc_nvenc: bool) -> dict:
    """Pick a primary recommendation. Returns codec + encoder_id + reasoning."""
    # Already AV1 + low compressibility → suggest staying with H.264 for compatibility
    if codec == "av1" and comp in ("moderate", "already_efficient"):
        return {
            "codec": "h264",
            "encoder_id": "h264_nvenc" if has_av1_nvenc else "libx264",  # NVENC presence implies modern GPU
            "reasoning": (
                "Source is already AV1; recompressing to AV1 yields little. "
                "Re-encode to H.264 only if you need broader playback compatibility — otherwise keep the original."
            ),
        }
    # Already efficient codec + low headroom → keep or move down for compatibility
    if source_eff in ("efficient", "very_efficient") and comp == "already_efficient":
        return {
            "codec": codec if codec in ("h264", "hevc", "av1", "vp9") else "h264",
            "encoder_id": "libx264",
            "reasoning": "File is already near its compressibility floor — heavy re-encoding mostly costs quality with little size win.",
        }
    # Inefficient source, lots to gain → AV1 (best efficiency)
    if source_eff in ("very_inefficient", "inefficient"):
        if has_av1_nvenc:
            return {
                "codec": "av1",
                "encoder_id": "av1_nvenc",
                "reasoning": "Source codec is dated and the file is highly compressible. AV1 gives the best size at quality, and your GPU supports av1_nvenc for fast hardware encoding.",
            }
        return {
            "codec": "av1",
            "encoder_id": "libsvtav1",
            "reasoning": "Source codec is dated and the file is highly compressible. SVT-AV1 produces the smallest file at a given VMAF among production encoders.",
        }
    # H.264 source, decent headroom → H.265 (good balance) or AV1 (max efficiency)
    if codec == "h264" and comp in ("highly_compressible", "compressible"):
        if has_av1_nvenc:
            return {
                "codec": "av1",
                "encoder_id": "av1_nvenc",
                "reasoning": "Re-encoding H.264 to AV1 typically saves 30–50% at the same VMAF. av1_nvenc gives near-software quality at GPU speeds.",
            }
        if has_hevc_nvenc:
            return {
                "codec": "hevc",
                "encoder_id": "hevc_nvenc",
                "reasoning": "Re-encoding H.264 to HEVC saves 25–40% at the same VMAF with broad device playback support.",
            }
        return {
            "codec": "av1",
            "encoder_id": "libsvtav1",
            "reasoning": "Re-encoding H.264 to AV1 with SVT-AV1 typically saves 30–50% at the same VMAF.",
        }
    # HEVC source with headroom → AV1
    if codec == "hevc" and comp in ("highly_compressible", "compressible"):
        return {
            "codec": "av1",
            "encoder_id": "av1_nvenc" if has_av1_nvenc else "libsvtav1",
            "reasoning": "HEVC source with re-encode headroom — AV1 typically saves 15–30% more at the same VMAF.",
        }
    # Default fallback
    return {
        "codec": "av1",
        "encoder_id": "av1_nvenc" if has_av1_nvenc else "libsvtav1",
        "reasoning": "AV1 is the best modern choice for size at quality; SVT-AV1 (or av1_nvenc on GPU) is the fastest path to good results.",
    }


def analyze(path: str) -> VideoInfo:
    caps = get_capabilities()
    if not caps.ffprobe:
        raise RuntimeError("ffprobe not available")

    probe = _run_ffprobe(caps.ffprobe, path)

    fmt = probe.get("format", {})
    streams = probe.get("streams", [])
    vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not vstream:
        raise RuntimeError("No video stream found in file")

    size_bytes = int(fmt.get("size") or Path(path).stat().st_size)
    duration = float(fmt.get("duration") or vstream.get("duration") or 0.0)
    bitrate = float(fmt.get("bit_rate") or vstream.get("bit_rate") or 0.0) / 1000.0
    if not bitrate and duration > 0:
        bitrate = (size_bytes * 8 / duration) / 1000.0

    width = int(vstream.get("width") or 0)
    height = int(vstream.get("height") or 0)
    fps = _parse_fps(vstream.get("avg_frame_rate") or vstream.get("r_frame_rate") or "0/0")
    codec = (vstream.get("codec_name") or "unknown").lower()
    pix_fmt = vstream.get("pix_fmt") or ""

    pixel_rate = max(1, width * height * max(1.0, fps))
    bpp = (bitrate * 1000.0) / pixel_rate if bitrate else 0.0

    eff = EFFICIENCY.get(codec, "moderate")
    comp, comp_label = _classify_compressibility(bpp, eff)
    eff_label = EFFICIENCY_LABEL[eff]

    container = (fmt.get("format_name") or "").split(",")[0]
    savings = _expected_savings(comp, eff)

    rec = _recommend(
        codec, eff, comp,
        has_av1_nvenc=caps.nvenc.get("av1", False),
        has_hevc_nvenc=caps.nvenc.get("hevc", False),
    )

    info_kwargs = dict(
        path=path,
        filename=Path(path).name,
        size_bytes=size_bytes,
        container=container,
        duration_s=duration,
        codec=codec,
        codec_label=codec.upper(),
        width=width,
        height=height,
        fps=round(fps, 3),
        bitrate_kbps=round(bitrate, 1),
        pix_fmt=pix_fmt,
        bpp=round(bpp, 4),
        compressibility=comp,
        compressibility_label=comp_label,
    )
    info_kwargs["verdict"] = _build_verdict(info_kwargs, eff_label, comp_label, savings)
    info_kwargs["recommendation"] = rec

    return VideoInfo(**info_kwargs)


def info_to_dict(info: VideoInfo) -> dict:
    return asdict(info)
