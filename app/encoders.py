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
    # Optional ffmpeg video-filter chain applied to the source before
    # encoding. Used to bake in pre-processing (denoise, etc) that gives
    # specific encoder presets their characteristic look. The filter is
    # passed via `--vfilter` to ab-av1 and via `--ffmpeg "-vf …"` to av1an.
    pre_filter: Optional[str] = None
    # When set, ab-av1's `-e` and the host capability lookup use this
    # instead of `id`. Lets us add multiple "presets" that share an
    # underlying ffmpeg encoder (e.g. libsvtav1 vs libsvtav1-tiny).
    ffmpeg_encoder: Optional[str] = None
    # Surfaced under the encoder card as a small note about the trade-off.
    notes: Optional[str] = None
    # If set, the UI auto-selects this VMAF target whenever this preset
    # is chosen. Use for presets where the default VMAF (90) doesn't
    # match the design point (e.g. low-bitrate delivery presets).
    recommended_vmaf_target: Optional[int] = None
    # av1an's `--passes` value. Defaults to 1 (single-pass) for backwards
    # compatibility with the existing presets. 2-pass with libsvtav1's
    # turbo first pass is meaningfully better at low bitrates.
    av1an_passes: int = 1
    # When set, the encoder skips av1an's per-chunk VMAF target search and
    # encodes directly at this CRF. The user's chosen target VMAF becomes
    # informational only — useful for delivery presets where the trade-off
    # is fixed by design (e.g. CRF 50 always lands ~450 kbps on 720p60
    # talking-head content with SVT-AV1 v4 preset 5).
    fixed_crf: Optional[int] = None
    # av1an `--min-q` / `--max-q` bounds for the target-quality CRF search.
    # Set BOTH to bias the search toward a particular operating point: e.g.
    # min_q=38 / max_q=55 makes av1an test the high-CRF (small-file) end
    # first and only drop CRF if the VMAF target isn't met. None = av1an
    # uses its built-in defaults (typically 0..63).
    av1an_min_q: Optional[int] = None
    av1an_max_q: Optional[int] = None

    @property
    def real_encoder(self) -> str:
        return self.ffmpeg_encoder or self.id

    def to_dict(self) -> dict:
        d = asdict(self)
        d["real_encoder"] = self.real_encoder
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
        preset="4",
        # Preset 4 is the slow side of "fast enough" — ~10-13% better
        # compression than preset 6 at ~3× encode time on typical content.
        # av1an's probe encodes still run at SVT-AV1's default fast preset
        # (preset 8), so the slowdown only affects the final encode.
        ab_av1_extra=["--preset", "4", "--pix-format", "yuv420p10le", "--min-crf", "18", "--max-crf", "45"],
        av1an_encoder="svt-av1",
        av1an_params="--preset 4 --keyint 240 --tune 0",
        description="Best practical AV1 encoder. Outstanding size-at-VMAF, runs on CPU.",
    ),
    Encoder(
        id="libsvtav1-tiny",
        label="AV1 — Tiny (delivery, SVT-AV1 v4)",
        codec="av1", container="mkv", type="sw",
        # av1an-only is now a soft constraint, not a hard technical one.
        # The original reason — "ffmpeg links the stale system SVT-AV1
        # v2.1.2; only av1an's direct SvtAv1EncApp call reaches v4" — no
        # longer holds: bin/ffmpeg is the BtbN build and links SVT-AV1
        # v4.1.0. ab-av1 drives that ffmpeg and reproduces this preset
        # (tested 2026-05-18: CRF ~49 → ~479 kbps at the VMAF target on
        # vidyo4, comparable to av1an's ~340-460 kbps on the same source).
        # CAVEAT before flipping `backends` to include ab-av1: ab-av1 has
        # no --ffmpeg flag — it resolves `ffmpeg` from PATH — and the
        # ab-av1 backend (app/backends/ab_av1.py) spawns it WITHOUT a PATH
        # override. On Linux it therefore picks up the distro ffmpeg, not
        # bin/ffmpeg, which links old SVT and lacks libvmaf (VMAF probes
        # then fail outright). Fix that first (spawn ab-av1 with bin/ on
        # PATH) or this preset will silently use the wrong encoder.
        # NOTE: SVT-AV1 v4 explicitly rejects multi-pass with CRF
        # ("CRF does not support multi-pass"); HandBrake's `MultiPass: true`
        # was a no-op for this codec. Single-pass CRF + preset 4 + v4 is
        # the actual recipe.
        backends=["av1an"],
        preset="4",
        ffmpeg_encoder="libsvtav1",
        # ab-av1 path is intentionally inert (av1an-only preset).
        ab_av1_extra=[
            "--preset", "4", "--pix-format", "yuv420p",
            "--min-crf", "38", "--max-crf", "55",
            "--keyint", "5s",
        ],
        av1an_encoder="svt-av1",
        # Preset 4 + GOP 320 (~5s @ 60fps). NOTE no `--crf` here —
        # av1an_min_q / av1an_max_q below bias av1an's --target-quality
        # binary search toward CRF 50ish so easy content (talking heads)
        # lands at CRF ~50 (matching HandBrake's 450 kbps), and harder
        # content drops down to the lower CRF bound only if needed to
        # honestly meet the VMAF target.
        av1an_params=(
            "--preset 4 --keyint 320 --tune 0 --enable-tf 1 --scd 1"
        ),
        # Search range for av1an --target-quality. min_q=38 / max_q=55
        # tests the high-CRF (small-file) end first; the binary search
        # picks the highest CRF that meets the user's target VMAF (default
        # 85 for this preset — see recommended_vmaf_target). For talking-
        # head content this lands on CRF ~50 ≈ 450 kbps; for more complex
        # content it drops to ~CRF 42 ≈ 1 Mbps.
        av1an_min_q=38,
        av1an_max_q=55,
        # No pre-filter. SVT-AV1 v4 + preset 5 + 2-pass handles smooth
        # content efficiently without denoising. Filtering would only hurt
        # detail retention and the encoder doesn't need the help anymore.
        pre_filter=None,
        description=(
            "Bandwidth-optimised AV1 (SVT-AV1 v4.1.0, preset 4). av1an "
            "probes the CRF 38-55 range biased toward high CRF — easy "
            "content lands at CRF ~50 (~450 kbps on talking-head 720p), "
            "harder content drops down only as needed to honestly meet "
            "the VMAF target. No enhancement tricks."
        ),
        notes=(
            "Auto-targets VMAF 85; backend auto-switches to av1an. Probes "
            "the high-CRF end first, so you get a small file when the "
            "content is well-compressible and a slightly bigger file "
            "(still tiny vs source) when it isn't."
        ),
        recommended_vmaf_target=85,
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
            if not caps.sw_encoders.get(enc.real_encoder, False):
                available = False
                reasons.append(f"ffmpeg encoder {enc.real_encoder} not built in")
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
