"""Backend protocol shared by ab-av1 and Av1an wrappers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..progress import JobState
from ..encoders import Encoder


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
