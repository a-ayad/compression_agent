"""Catalog of supported encoders, with per-backend invocation details.

Each encoder entry describes:
- id            : ffmpeg encoder name (also the ab-av1 -e value)
- label         : display name
- codec         : h264 | hevc | av1 | vp9
- container     : preferred output container
- type          : "hw" | "sw"
- backends      : which backends can drive this encoder
- ab_av1_extra  : list of extra args appended to ab-av1
- av1an_encoder : av1an's --encoder value (None if unsupported by av1an)
- av1an_params  : default --video-params string for av1an (encoder-native flags)
- gpu_required  : True for NVENC entries
- preset        : default speed preset (sent via ab-av1 --preset, or in av1an_params)
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

from .tools import get_capabilities


@dataclass
class Encoder:
    id: str
    label: str
    codec: str
    container: str
    type: str  # "hw" | "sw"
    backends: list
    preset: str
    ab_av1_extra: list
    av1an_encoder: Optional[str]
    av1an_params: Optional[str]
    gpu_required: bool = False
    description: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# NOTE: ab-av1 takes encoder args via repeated `--enc key=value`. For NVENC we
# use VBR rate control with -cq driven by ab-av1's internal CRF→CQ mapping. For
# software encoders we let ab-av1 pass --crf naturally.

CATALOG: list[Encoder] = [
    # ─── AV1 ───────────────────────────────────────────────────────────────
    Encoder(
        id="libsvtav1",
        label="AV1 — SVT-AV1 (software)",
        codec="av1", container="mkv", type="sw",
        backends=["ab-av1", "av1an"],
        preset="6",
        ab_av1_extra=["--preset", "6", "--pix-format", "yuv420p10le", "--min-crf", "18", "--max-crf", "45"],
        av1an_encoder="svt-av1",
        av1an_params="--preset 6 --keyint 240 --tune 0",
        description="Best practical AV1 encoder. Outstanding size-at-VMAF, runs on CPU.",
    ),
    Encoder(
        id="av1_nvenc",
        label="AV1 — NVENC (hardware, RTX 40+)",
        codec="av1", container="mkv", type="hw",
        backends=["ab-av1"],
        preset="p7",
        ab_av1_extra=[
            "--preset", "p7",
            "--enc", "tune=hq",
            "--enc", "rc=vbr",
            "--enc", "multipass=fullres",
            "--min-crf", "18",
            # NVENC quality drops fast above ~35; sample-based prediction at higher CRFs is unreliable.
            "--max-crf", "36",
        ],
        av1an_encoder=None, av1an_params=None,
        gpu_required=True,
        description="Hardware AV1 on RTX 40-series and newer. Very fast, near-software quality.",
    ),
    # ─── HEVC / H.265 ─────────────────────────────────────────────────────
    Encoder(
        id="libx265",
        label="H.265 — x265 (software)",
        codec="hevc", container="mkv", type="sw",
        backends=["ab-av1", "av1an"],
        preset="slow",
        ab_av1_extra=["--preset", "slow", "--min-crf", "16", "--max-crf", "34"],
        av1an_encoder="x265",
        av1an_params="--preset slow --keyint 240",
        description="x265 'slow' preset is a strong size-at-VMAF baseline for HEVC.",
    ),
    Encoder(
        id="hevc_nvenc",
        label="H.265 — NVENC (hardware)",
        codec="hevc", container="mkv", type="hw",
        backends=["ab-av1"],
        preset="p7",
        ab_av1_extra=[
            "--preset", "p7",
            "--enc", "tune=hq",
            "--enc", "rc=vbr",
            "--enc", "multipass=fullres",
            "--enc", "spatial_aq=1",
            "--min-crf", "16",
            "--max-crf", "33",
        ],
        av1an_encoder=None, av1an_params=None,
        gpu_required=True,
        description="Hardware HEVC. Massively faster than x265 with a small quality hit.",
    ),
    # ─── H.264 ────────────────────────────────────────────────────────────
    Encoder(
        id="libx264",
        label="H.264 — x264 (software)",
        codec="h264", container="mp4", type="sw",
        backends=["ab-av1", "av1an"],
        preset="slow",
        ab_av1_extra=["--preset", "slow", "--min-crf", "14", "--max-crf", "30"],
        av1an_encoder="x264",
        av1an_params="--preset slow --keyint 240",
        description="The universal compatibility option. Plays everywhere.",
    ),
    Encoder(
        id="h264_nvenc",
        label="H.264 — NVENC (hardware)",
        codec="h264", container="mp4", type="hw",
        backends=["ab-av1"],
        preset="p7",
        ab_av1_extra=[
            "--preset", "p7",
            "--enc", "tune=hq",
            "--enc", "rc=vbr",
            "--enc", "multipass=fullres",
            "--enc", "spatial_aq=1",
            "--min-crf", "14",
            "--max-crf", "30",
        ],
        av1an_encoder=None, av1an_params=None,
        gpu_required=True,
        description="Hardware H.264. Real-time speeds with good quality.",
    ),
    # ─── VP9 ──────────────────────────────────────────────────────────────
    Encoder(
        id="libvpx-vp9",
        label="VP9 — libvpx (software)",
        codec="vp9", container="webm", type="sw",
        backends=["ab-av1", "av1an"],
        preset="2",  # cpu-used 2 ≈ slow preset
        ab_av1_extra=[
            "--preset", "2",
            "--enc", "row-mt=1",
            "--enc", "tile-columns=2",
            "--min-crf", "15",
            "--max-crf", "40",
        ],
        av1an_encoder="vpx",
        av1an_params="--cpu-used=2 --row-mt=1 --tile-columns=2 --end-usage=q",
        description="Royalty-free, broad WebM support. Slower to encode than x265.",
    ),
]


def list_encoders() -> list[dict]:
    """Return catalog filtered by what the host can actually run."""
    caps = get_capabilities()
    out = []
    for enc in CATALOG:
        available = True
        reasons = []
        if enc.gpu_required:
            kind = enc.codec  # "av1" | "hevc" | "h264"
            if not caps.nvenc.get(kind, False):
                available = False
                reasons.append(f"NVENC {kind.upper()} not detected on this GPU")
        else:
            if not caps.sw_encoders.get(enc.id, False):
                available = False
                reasons.append(f"ffmpeg encoder {enc.id} not built in")
        d = enc.to_dict()
        d["available"] = available
        d["unavailable_reason"] = "; ".join(reasons) if reasons else None
        out.append(d)
    return out


def get_encoder(encoder_id: str) -> Encoder:
    for e in CATALOG:
        if e.id == encoder_id:
            return e
    raise KeyError(f"Unknown encoder: {encoder_id}")
