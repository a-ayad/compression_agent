"""ab-av1 backend.

Wraps the `ab-av1 auto-encode` two-phase command (CRF search + final encode)
and translates its stderr output into structured JobEvents.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Optional

from ..progress import JobEvent, JobState
from ..tools import get_capabilities
from .base import Backend, EncodeRequest


# Regex patterns to match ab-av1 stderr (which mixes its own messages with
# forwarded ffmpeg output during the encode phase).

RE_SAMPLE_PROGRESS = re.compile(r"encoding sample\s+(\d+)/(\d+)\s+crf\s+([\d.]+)", re.I)
# Format: "crf 32 VMAF 97.90 predicted video stream size 6.36 MiB (44%) taking 1 second"
RE_CRF_VMAF = re.compile(
    r"\bcrf\s+([\d.]+)\s+VMAF\s+([\d.]+)\s+predicted[^(]*\(([\d.]+)%\)",
    re.I,
)
# Final encode kickoff: "[... ab_av1::command::encode] encoding <output_filename>"
RE_ENCODE_STARTED = re.compile(r"\bcommand::encode\b.*?\bencoding\s+\S+", re.I)
RE_FFMPEG_TIME = re.compile(r"time=(\d+):(\d+):([\d.]+)")
RE_FFMPEG_FRAME = re.compile(r"frame=\s*(\d+)")
# ab-av1 reports running encode progress as "Encoded X.YY MiB (PCT%)" where PCT
# is encoded-vs-original size, NOT progress through the file. Useful as a
# liveness signal but not a true % done.
RE_ENCODE_LIVENESS = re.compile(r"^Encoded\s+[\d.]+\s+\w+\s+\(([\d.]+)%\)", re.I)
RE_ERROR = re.compile(r"\b(error|fatal)\b", re.I)


class AbAv1Backend:
    name = "ab-av1"

    async def is_available(self) -> tuple[bool, str]:
        caps = get_capabilities()
        if not caps.ab_av1:
            return False, "ab-av1 binary not available (auto-download failed?)"
        if not caps.ffmpeg:
            return False, "ffmpeg not available"
        return True, ""

    def _build_command(self, req: EncodeRequest, duration_s: float) -> list[str]:
        caps = get_capabilities()
        enc = req.encoder
        cmd = [
            caps.ab_av1,
            "auto-encode",
            "-i", req.input_path,
            "-o", req.output_path,
            "-e", enc.id,
            "--min-vmaf", f"{req.target_vmaf:.1f}",
            "--max-encoded-percent", "100",  # don't fail just because file isn't smaller
        ]
        cmd.extend(enc.ab_av1_extra)

        # ab-av1's auto-encode default keyint is 10s if input > 3min — fine.
        # For very short clips force at least 2 samples to get a sensible CRF.
        if duration_s and duration_s < 60:
            cmd.extend(["--samples", "2", "--sample-duration", "10s"])
        elif duration_s and duration_s < 240:
            cmd.extend(["--samples", "3"])
        return cmd

    async def encode(self, req: EncodeRequest, job: JobState) -> dict:
        from ..analysis import analyze  # local import to avoid cycle

        ok, why = await self.is_available()
        if not ok:
            await job.emit(JobEvent(type="error", stage="error", message=why))
            raise RuntimeError(why)

        info = analyze(req.input_path)
        duration_s = info.duration_s

        cmd = self._build_command(req, duration_s)
        await job.emit(JobEvent(
            type="log", stage="searching",
            message=f"Launching ab-av1 with encoder {req.encoder.id} (target VMAF {req.target_vmaf})",
            data={"command": " ".join(f'"{c}"' if " " in c else c for c in cmd)},
        ))
        await job.emit(JobEvent(
            type="stage", stage="searching", percent=2,
            message="Searching for the lowest CRF that meets the VMAF target…",
        ))

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(Path(req.output_path).parent),
        )

        state = {
            "stage": "searching",
            "samples_done": 0,
            "samples_total": 0,
            "search_attempts": 0,
            "best_crf": None,
            "best_vmaf": None,
            "predicted_size_pct": None,
            "predicted_size_human": None,
            "encode_started": False,
            "last_emit_ts": 0.0,
        }

        async def emit_throttled(ev: JobEvent, min_interval: float = 0.25) -> None:
            now = time.time()
            if now - state["last_emit_ts"] >= min_interval or ev.type != "progress":
                state["last_emit_ts"] = now
                await job.emit(ev)

        assert process.stdout is not None
        full_stderr_lines: list[str] = []
        while True:
            raw = await process.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue
            full_stderr_lines.append(line)
            await self._handle_line(line, state, duration_s, job, emit_throttled)

        rc = await process.wait()
        if rc != 0:
            tail = "\n".join(full_stderr_lines[-25:])
            await job.emit(JobEvent(
                type="error", stage="error",
                message=f"ab-av1 exited with code {rc}",
                data={"tail": tail, "command": cmd},
            ))
            raise RuntimeError(f"ab-av1 failed with code {rc}\n{tail}")

        out_path = Path(req.output_path)
        if not out_path.exists() or out_path.stat().st_size == 0:
            await job.emit(JobEvent(
                type="error", stage="error",
                message="ab-av1 finished but output file is missing or empty",
            ))
            raise RuntimeError("Output missing after ab-av1 run")

        result = {
            "output_path": str(out_path),
            "output_size": out_path.stat().st_size,
            "best_crf": state["best_crf"],
            "predicted_vmaf": state["best_vmaf"],
            "predicted_size_pct": state["predicted_size_pct"],
        }
        await job.emit(JobEvent(
            type="stage", stage="encoded", percent=92,
            message="Encoding complete. Measuring final VMAF…",
            data=result,
        ))
        return result

    async def _handle_line(self, line: str, state: dict, duration_s: float,
                            job: JobState, emit_throttled) -> None:
        # Always send the raw line as a log event so the UI can show a tail.
        await job.emit(JobEvent(type="log", stage=state["stage"], message=line))

        # ── Search phase: sample N/M starting at crf X ──
        m = RE_SAMPLE_PROGRESS.search(line)
        if m and not state["encode_started"]:
            done, total = int(m.group(1)), int(m.group(2))
            state["samples_done"] = done
            state["samples_total"] = max(total, state["samples_total"])
            await job.emit(JobEvent(
                type="progress", stage="searching",
                percent=min(40.0, 5.0 + state["search_attempts"] * 10.0 + 2.0),
                message=f"Sampling at CRF {m.group(3)}… ({done}/{total})",
            ))
            return

        # ── Search phase: each CRF probe result ──
        m = RE_CRF_VMAF.search(line)
        if m and not state["encode_started"]:
            crf = float(m.group(1))
            vmaf = float(m.group(2))
            pct = float(m.group(3))
            state["search_attempts"] += 1
            state["best_crf"] = crf
            state["best_vmaf"] = vmaf
            state["predicted_size_pct"] = pct
            search_pct = min(45.0, 5.0 + state["search_attempts"] * 10.0)
            await job.emit(JobEvent(
                type="progress", stage="searching", percent=search_pct,
                message=f"CRF {crf:g} → predicted VMAF {vmaf:.2f} ({pct:.1f}% of input size)",
                data={"crf": crf, "vmaf": vmaf, "predicted_pct": pct},
            ))
            return

        # ── Transition: final encode begins ──
        if not state["encode_started"] and RE_ENCODE_STARTED.search(line):
            state["encode_started"] = True
            state["stage"] = "encoding"
            await job.emit(JobEvent(
                type="stage", stage="encoding", percent=50,
                message=f"Encoding final output (CRF {state['best_crf']:g})…",
                data={"crf": state["best_crf"]},
            ))
            return

        # ── Encode phase: ffmpeg time= for true progress ──
        if state["encode_started"]:
            m = RE_FFMPEG_TIME.search(line)
            if m and duration_s > 0:
                hh, mm, ss = int(m.group(1)), int(m.group(2)), float(m.group(3))
                cur = hh * 3600 + mm * 60 + ss
                ratio = max(0.0, min(1.0, cur / duration_s))
                pct = 50.0 + ratio * 40.0  # 50→90%
                await emit_throttled(JobEvent(
                    type="progress", stage="encoding", percent=pct,
                    message=f"Encoded {cur:.1f}s / {duration_s:.1f}s",
                ))
                return
            # Fallback liveness — at least show that something is happening
            m = RE_ENCODE_LIVENESS.search(line)
            if m:
                await emit_throttled(JobEvent(
                    type="progress", stage="encoding", percent=70,
                    message=f"Encoding (output is {m.group(1)}% of original size)",
                ))
                return

        # ── Errors ──
        if RE_ERROR.search(line) and "warning" not in line.lower():
            await job.emit(JobEvent(
                type="log", stage=state["stage"],
                message=f"⚠ {line}",
            ))
