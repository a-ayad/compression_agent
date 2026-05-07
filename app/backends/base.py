"""Backend protocol shared by ab-av1 and Av1an wrappers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..progress import JobState
from ..encoders import Encoder


# How much to bump the user's target VMAF when feeding it to a backend's
# target-quality search. Both ab-av1 (sample-encode CRF probing) and
# av1an (per-chunk binary search) measure VMAF on short normalised
# samples, which scores systematically higher than a full-clip honest
# measurement on motion-heavy content (DJI drone footage was the
# original prompt — the predictor said 90 while libvmaf on the full
# file said 65). Compensating at the target keeps the post-encode
# `measure_vmaf` reading near the user's intent.
#
# Tune carefully: too high and easy content gets larger files than
# needed; too low and motion content keeps undershooting. 4 is the
# midpoint of the 2..7 range we've observed across content types.
PREDICTOR_VMAF_OFFSET = 4.0


def predictor_target(user_target: float, encoder: Encoder) -> float:
    """User-facing VMAF target → backend-internal probe target.

    Capped at 99 so the backend's own (0..100] domain doesn't reject it.
    Encoders flagged with `fixed_crf` skip target-quality probing, so
    the offset is irrelevant for those — we still return the raw value
    rather than +offset to avoid surprising downstream consumers.
    """
    if getattr(encoder, "fixed_crf", None) is not None:
        return user_target
    return min(99.0, user_target + PREDICTOR_VMAF_OFFSET)


@dataclass
class EncodeRequest:
    input_path: str
    output_path: str
    encoder: Encoder
    target_vmaf: float          # 85, 90, 95
    extra_options: dict         # backend-specific tuning, e.g. {"workers": 4}


class Backend(Protocol):
    name: str

    async def is_available(self) -> tuple[bool, str]:
        """Return (ok, reason_if_not)."""
        ...

    async def encode(self, req: EncodeRequest, job: JobState) -> dict:
        """Run encode. Emit progress events into job. Return result dict on success."""
        ...
