"""Av1an backend.

Wraps `av1an` for scene-detected, chunked, parallel target-quality encoding.
Software encoders only (Av1an does not support NVENC).
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Optional

from ..progress import JobEvent, JobState
from ..tools import get_capabilities
from .base import Backend, EncodeRequest


RE_SCENES_DETECTED = re.compile(r"(?:detected|found)\s+(\d+)\s+scenes?", re.I)
RE_CHUNK_DONE = re.compile(r"chunk\s+(\d+)\s*/\s*(\d+)", re.I)
RE_ENCODED_FRAMES = re.compile(r"(\d+)\s*/\s*(\d+)\s+frames?", re.I)
RE_FPS = re.compile(r"(\d+(?:\.\d+)?)\s*fps", re.I)
RE_PROGRESS_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
RE_ERROR = re.compile(r"\b(error|fatal|panicked)\b", re.I)


class Av1anBackend:
    name = "av1an"

    async def is_available(self) -> tuple[bool, str]:
        caps = get_capabilities()
        if not caps.av1an:
            return False, (
                "av1an not installed. Install via: scoop install av1an  (or build from source); "
                "also requires VapourSynth R72 + L-SMASH/FFMS2 plugins on Windows."
            )
        if not caps.ffmpeg:
            return False, "ffmpeg not available"
        if not caps.vapoursynth:
            return False, "VapourSynth not detected (av1an needs it for chunk splitting)"
        return True, ""

    def _build_command(self, req: EncodeRequest, workers: int) -> list[str]:
        caps = get_capabilities()
        enc = req.encoder
        if not enc.av1an_encoder:
            raise ValueError(f"{enc.id} is not supported by Av1an (NVENC encoders only run via ab-av1)")

        cmd = [
            caps.av1an,
            "-i", req.input_path,
            "-o", req.output_path,
            "--encoder", enc.av1an_encoder,
            "--video-params", enc.av1an_params or "",
            "--target-quality", f"{req.target_vmaf:.1f}",
            "--target-metric", "vmaf",
            "-w", str(workers),
            "--concat", "mkvmerge",
            "--audio-params", "-c:a libopus -b:a 128k",
            "--verbose",
        ]
        return cmd

    async def encode(self, req: EncodeRequest, job: JobState) -> dict:
        from ..analysis import analyze
        ok, why = await self.is_available()
        if not ok:
            await job.emit(JobEvent(type="error", stage="error", message=why))
            raise RuntimeError(why)

        info = analyze(req.input_path)
        workers = int(req.extra_options.get("workers") or self._auto_workers())
        cmd = self._build_command(req, workers)

        await job.emit(JobEvent(
            type="log", stage="searching",
            message=f"Launching av1an with encoder {req.encoder.av1an_encoder} ({workers} workers, target VMAF {req.target_vmaf})",
            data={"command": " ".join(f'"{c}"' if " " in c else c for c in cmd), "workers": workers},
        ))
        await job.emit(JobEvent(
            type="stage", stage="searching", percent=2,
            message="Detecting scenes for parallel encoding…",
        ))

        env = dict(os.environ)
        env["TERM"] = "dumb"  # try to suppress ANSI cursor games
        env["RUST_LOG"] = env.get("RUST_LOG", "info")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(Path(req.output_path).parent),
            env=env,
        )

        state = {
            "stage": "searching",
            "scenes_total": 0,
            "chunks_done": 0,
            "encode_started": False,
            "last_emit_ts": 0.0,
        }

        async def emit_throttled(ev: JobEvent, min_interval: float = 0.4) -> None:
            now = time.time()
            if now - state["last_emit_ts"] >= min_interval or ev.type != "progress":
                state["last_emit_ts"] = now
                await job.emit(ev)

        assert process.stdout is not None
        full_lines: list[str] = []
        while True:
            raw = await process.stdout.readline()
            if not raw:
                break
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            # av1an's progress bar uses \r — split that out too
            for line in text.split("\r"):
                line = line.strip()
                if not line:
                    continue
                full_lines.append(line)
                await self._handle_line(line, state, info.duration_s, job, emit_throttled)

        rc = await process.wait()
        if rc != 0:
            tail = "\n".join(full_lines[-30:])
            await job.emit(JobEvent(
                type="error", stage="error",
                message=f"av1an exited with code {rc}",
                data={"tail": tail, "command": cmd},
            ))
            raise RuntimeError(f"av1an failed with code {rc}\n{tail}")

        out_path = Path(req.output_path)
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError("Output missing after av1an run")

        result = {
            "output_path": str(out_path),
            "output_size": out_path.stat().st_size,
            "scenes": state["scenes_total"],
            "workers": workers,
        }
        await job.emit(JobEvent(
            type="stage", stage="encoded", percent=92,
            message="Encoding complete. Measuring final VMAF…",
            data=result,
        ))
        return result

    async def _handle_line(self, line: str, state: dict, duration_s: float,
                            job: JobState, emit_throttled) -> None:
        await job.emit(JobEvent(type="log", stage=state["stage"], message=line))

        m = RE_SCENES_DETECTED.search(line)
        if m:
            state["scenes_total"] = int(m.group(1))
            state["stage"] = "encoding"
            await job.emit(JobEvent(
                type="stage", stage="encoding", percent=15,
                message=f"Detected {state['scenes_total']} scenes — starting parallel encode",
                data={"scenes": state["scenes_total"]},
            ))
            return

        m = RE_CHUNK_DONE.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            state["chunks_done"] = max(done, state["chunks_done"])
            state["scenes_total"] = max(total, state["scenes_total"])
            ratio = done / max(1, total)
            pct = 15.0 + ratio * 75.0  # 15→90%
            await emit_throttled(JobEvent(
                type="progress", stage="encoding", percent=pct,
                message=f"Chunk {done}/{total} complete",
                data={"chunks_done": done, "chunks_total": total},
            ))
            return

        m = RE_ENCODED_FRAMES.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            ratio = done / max(1, total)
            pct = 15.0 + ratio * 75.0
            await emit_throttled(JobEvent(
                type="progress", stage="encoding", percent=pct,
                message=f"Encoded {done}/{total} frames",
            ))
            return

        if RE_ERROR.search(line):
            await job.emit(JobEvent(type="log", stage=state["stage"], message=f"⚠ {line}"))

    @staticmethod
    def _auto_workers() -> int:
        try:
            cpu = os.cpu_count() or 4
        except Exception:
            cpu = 4
        # Keep a couple of cores for the OS / single-thread bottlenecks
        return max(2, min(12, cpu - 2))
