"""Tool detection and auto-vendoring of ab-av1.

Discovers ffmpeg/ffprobe, ab-av1, av1an, and VapourSynth at startup.
Auto-downloads ab-av1.exe from GitHub releases if not present.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

import httpx

ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = ROOT / "bin"
COMMON_FFMPEG_PATHS = [
    Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
    Path(r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"),
    Path("/usr/bin/ffmpeg"),
    Path("/usr/local/bin/ffmpeg"),
    Path("/opt/ffmpeg/bin/ffmpeg"),
]


@dataclass
class Capabilities:
    ffmpeg: Optional[str] = None
    ffprobe: Optional[str] = None
    ab_av1: Optional[str] = None
    av1an: Optional[str] = None
    vapoursynth: bool = False
    nvenc: dict = field(default_factory=lambda: {"h264": False, "hevc": False, "av1": False})
    sw_encoders: dict = field(default_factory=lambda: {
        "libx264": False, "libx265": False, "libsvtav1": False, "libvpx-vp9": False,
    })
    libvmaf: bool = False
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _which(name: str) -> Optional[str]:
    p = shutil.which(name)
    return p


def _find_ffmpeg() -> tuple[Optional[str], Optional[str]]:
    # Project-local bin/ takes precedence — install.sh drops a static build here
    # that we know has libvmaf, libsvtav1, NVENC etc. The OS-shipped ffmpeg may
    # be missing those features.
    ff_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    fp_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    local_ff = BIN_DIR / ff_name
    local_fp = BIN_DIR / fp_name
    if local_ff.exists() and local_fp.exists():
        return str(local_ff), str(local_fp)

    ff = _which("ffmpeg")
    fp = _which("ffprobe")
    if ff and fp:
        return ff, fp

    for guess in COMMON_FFMPEG_PATHS:
        if guess.exists():
            ff = str(guess)
            fp = str(guess.with_name(fp_name))
            if Path(fp).exists():
                return ff, fp
    return None, None


def _ffmpeg_encoder_list(ffmpeg: str) -> str:
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=15,
        )
        return (out.stdout or "") + (out.stderr or "")
    except Exception:
        return ""


def _ffmpeg_filter_list(ffmpeg: str) -> str:
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=15,
        )
        return (out.stdout or "") + (out.stderr or "")
    except Exception:
        return ""


def _check_vapoursynth() -> bool:
    try:
        r = subprocess.run(
            [sys.executable, "-c", "import vapoursynth; print(vapoursynth.__version__)"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return True
    except Exception:
        pass
    if _which("vspipe"):
        return True
    return False


def _ensure_ab_av1() -> Optional[str]:
    """Return path to ab-av1.exe. If not on PATH, download to bin/."""
    on_path = _which("ab-av1") or _which("ab-av1.exe")
    if on_path:
        return on_path

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    target = BIN_DIR / ("ab-av1.exe" if os.name == "nt" else "ab-av1")
    if target.exists():
        return str(target)

    if os.name != "nt":
        return None

    print("Downloading ab-av1.exe from GitHub releases...", flush=True)
    try:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            api = client.get("https://api.github.com/repos/alexheretic/ab-av1/releases/latest")
            api.raise_for_status()
            release = api.json()
            asset_url = None
            for a in release.get("assets", []):
                if a["name"] == "ab-av1.exe":
                    asset_url = a["browser_download_url"]
                    break
            if not asset_url:
                return None
            with client.stream("GET", asset_url) as r:
                r.raise_for_status()
                with target.open("wb") as f:
                    for chunk in r.iter_bytes(1 << 16):
                        f.write(chunk)
        print(f"  → {target} ({target.stat().st_size / 1024:.0f} KB)", flush=True)
        return str(target)
    except Exception as e:
        print(f"  ! ab-av1 download failed: {e}", flush=True)
        return None


def detect() -> Capabilities:
    caps = Capabilities()

    ff, fp = _find_ffmpeg()
    if not ff or not fp:
        caps.errors.append("ffmpeg/ffprobe not found in PATH or common locations")
        return caps
    caps.ffmpeg = ff
    caps.ffprobe = fp

    encoders = _ffmpeg_encoder_list(ff)
    caps.nvenc["h264"] = "h264_nvenc" in encoders
    caps.nvenc["hevc"] = "hevc_nvenc" in encoders
    caps.nvenc["av1"] = "av1_nvenc" in encoders
    for name in caps.sw_encoders:
        caps.sw_encoders[name] = name in encoders

    caps.libvmaf = "libvmaf" in _ffmpeg_filter_list(ff)
    if not caps.libvmaf:
        caps.errors.append("ffmpeg lacks libvmaf filter — VMAF measurement disabled")

    caps.ab_av1 = _ensure_ab_av1()
    if not caps.ab_av1:
        caps.errors.append("ab-av1 not available (download failed?)")

    caps.av1an = _which("av1an") or _which("av1an.exe")
    caps.vapoursynth = _check_vapoursynth()

    return caps


_cached: Optional[Capabilities] = None


def get_capabilities(refresh: bool = False) -> Capabilities:
    global _cached
    if _cached is None or refresh:
        _cached = detect()
    return _cached


def tool_env(extra: Optional[dict] = None) -> dict:
    """Build a subprocess environment with the detected ffmpeg's directory
    prepended to PATH.

    ab-av1 has no --ffmpeg flag and av1an shells out to ffmpeg by name;
    both resolve `ffmpeg`/`ffprobe` from PATH. `_find_ffmpeg()` prefers the
    project-local static build (in bin/), but that preference is useless to
    a child process unless bin/ is actually on its PATH — otherwise the
    child silently picks up the distro ffmpeg, which may link an old
    SVT-AV1 and lack the libvmaf filter (VMAF probes then fail outright).

    Prepending here makes the child agree with `caps.ffmpeg`. `extra`
    overrides are applied last (e.g. TERM, RUST_LOG for av1an).
    """
    env = dict(os.environ)
    caps = get_capabilities()
    if caps.ffmpeg:
        ff_dir = str(Path(caps.ffmpeg).resolve().parent)
        parts = env.get("PATH", "").split(os.pathsep)
        if ff_dir not in parts:
            env["PATH"] = ff_dir + os.pathsep + env.get("PATH", "")
    if extra:
        env.update(extra)
    return env
