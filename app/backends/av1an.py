"""Av1an backend.

Wraps `av1an` for scene-detected, chunked, parallel target-quality encoding.
Software encoders only (Av1an does not support NVENC).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from ..progress import JobEvent, JobState
from ..tools import get_capabilities, tool_env
from .base import Backend, EncodeRequest, predictor_target


RE_SCENES_DETECTED = re.compile(r"(?:detected|found)\s+(\d+)\s+scenes?", re.I)
RE_CHUNK_DONE = re.compile(r"chunk\s+(\d+)\s*/\s*(\d+)", re.I)
RE_ENCODED_FRAMES = re.compile(r"(\d+)\s*/\s*(\d+)\s+frames?", re.I)
RE_FPS = re.compile(r"(\d+(?:\.\d+)?)\s*fps", re.I)
RE_PROGRESS_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
RE_ERROR = re.compile(r"\b(error|fatal|panicked)\b", re.I)

# Rich patterns from av1an's --log-level=debug output (only present in the
# log file, not stdout).
RE_LOG_SCENE_INFO = re.compile(
    r"scenecut: found\s+(\d+) scene\(s\) \[with extra_splits \((\d+) frames\):\s+(\d+) scene\(s\)\]"
)
RE_LOG_TQ_PROBES = re.compile(
    r"chunk (\d+):\s+TQ-Probes:\s+\[(.*?)\](?:\s+(.*))?$"
)
RE_LOG_TQ_TARGET = re.compile(
    r"chunk (\d+):\s+Target Q=(\d+),\s+VMAF=([\d.]+)"
)
RE_LOG_CHUNK_START = re.compile(
    r"started chunk (\d+):\s+(\d+) frames"
)
RE_LOG_CHUNK_DONE = re.compile(
    r"finished chunk (\d+):\s+(\d+) frames,\s+([\d.]+) fps,\s+took ([\d.]+)s"
)
RE_LOG_PHASE_CONCAT = re.compile(r"encoding finished, concatenating")
RE_LOG_TEMP_DIR = re.compile(r"temporary directory:\s+(\S+)")
RE_LOG_INPUT_INFO = re.compile(
    r"Input:\s+(\d+)x(\d+)\s+@\s+([\d.]+) fps,\s+(\S+),\s+(\S+)"
)


# Encoder → set of source pixel formats av1an will accept without
# rejecting at validation. `--pix-format` only controls the chunk format
# fed to the encoder; av1an still validates the demuxer-reported pix_fmt
# against this list before scene detection. Anything outside the set
# (UYVY422, NV12, RGB, …) needs a lossless pre-transcode upstream.
ENCODER_ACCEPTED_PIX_FMTS: dict[str, set[str]] = {
    "svt-av1": {"yuv420p", "yuv420p10le"},
    "x264": {
        "yuv420p", "yuv422p", "yuv444p",
        "yuv420p10le", "yuv422p10le", "yuv444p10le",
    },
    "x265": {
        "yuv420p", "yuv422p", "yuv444p",
        "yuv420p10le", "yuv422p10le", "yuv444p10le",
    },
    "vpx": {
        "yuv420p", "yuv422p", "yuv440p", "yuv444p",
        "yuv420p10le", "yuv422p10le", "yuv440p10le", "yuv444p10le",
    },
}


def _override_preset_in_params(params: str, new_preset: int) -> str:
    """Replace `--preset <N>` in an SVT-AV1 / x264 / x265 params string.
    No-op if `--preset` isn't present (e.g. NVENC encoders use `-preset`
    differently and aren't supported by this override path)."""
    return re.sub(r"(--preset\s+)\S+", rf"\g<1>{new_preset}", params)


async def _probe_pix_fmt(ffprobe: str, path: str) -> Optional[str]:
    """Return the demuxer-reported pix_fmt of the first video stream."""
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=pix_fmt",
            "-of", "json", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        data = json.loads((out or b"{}").decode() or "{}")
        s = (data.get("streams") or [{}])[0]
        return str(s.get("pix_fmt") or "") or None
    except Exception:
        return None


async def _ffv1_to_yuv420p(ffmpeg: str, src: str, dst: str) -> None:
    """Lossless rewrap of `src` as `yuv420p` ffv1 in matroska. Used to feed
    av1an a source whose pixel format would otherwise fail its validator.
    Audio is dropped — av1an gets video only and we mux audio separately."""
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", src,
        "-an",
        "-c:v", "ffv1", "-level", "3", "-coder", "1", "-context", "1",
        "-pix_fmt", "yuv420p",
        dst,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        tail = (err or b"").decode(errors="replace")[-1000:]
        raise RuntimeError(
            f"pre-transcode to yuv420p failed (rc={proc.returncode}):\n{tail}"
        )


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

    def _build_command(self, req: EncodeRequest, workers: int, input_path: str) -> list[str]:
        caps = get_capabilities()
        enc = req.encoder
        if not enc.av1an_encoder:
            raise ValueError(f"{enc.id} is not supported by Av1an (NVENC encoders only run via ab-av1)")

        # User-overridable preset (UI slider). Falls back to the encoder's
        # baked-in preset value when not set.
        params = enc.av1an_params or ""
        preset_override = req.extra_options.get("encoder_preset")
        if preset_override is not None and "--preset" in params:
            params = _override_preset_in_params(params, int(preset_override))

        # Per-job log file. av1an always appends `.log` to whatever
        # `--log-file` value we give, so we pass the stem and read
        # `<stem>.log` from the tail task. `--log-level debug` enables the
        # per-chunk probe / target-Q / fps lines we want to surface in the
        # UI; without it the whole encode emits 4 stdout lines and nothing
        # else.
        log_stem = f"{Path(req.output_path).with_suffix('')}_av1an_log"
        self._log_path = Path(f"{log_stem}.log")
        self._log_path.unlink(missing_ok=True)

        cmd = [
            caps.av1an,
            "-i", input_path,
            "-o", req.output_path,
            "--encoder", enc.av1an_encoder,
            "--video-params", params,
            "-w", str(workers),
            "--concat", "mkvmerge",
            "--audio-params", "-c:a libopus -b:a 128k",
            # Force chunk pixel format to 8-bit planar 4:2:0. Our encoder
            # params are 8-bit so there's no precision to preserve.
            "--pix-format", "yuv420p",
            "--log-file", log_stem,
            "--log-level", "debug",
            "--verbose",
        ]
        # Quality control: presets with `fixed_crf` encode directly at that
        # CRF (must already appear in av1an_params). Otherwise let av1an
        # search per-chunk for a CRF that hits the user's target VMAF.
        # `--probes 5` (default is 4) tightens the binary search — fewer
        # overshoots above the target VMAF at the cost of one extra sample
        # encode per chunk. av1an already uses a fast preset for these
        # probes and the user's `av1an_params` preset for the final encode,
        # so probe overhead is small.
        # `av1an_min_q` / `av1an_max_q` bias the search range — tightening
        # to (e.g.) 38..55 makes av1an check the high-CRF (small-file) end
        # first, picking CRF 50ish for easy content and only dropping CRF
        # for content that genuinely needs more bits.
        if getattr(enc, "fixed_crf", None) is None:
            # av1an's per-chunk VMAF probes run on samples and score a
            # few points higher than a full-clip honest measurement.
            # Target a slightly higher VMAF internally so the post-
            # encode measure_vmaf reading lands at the user's intent.
            probe_vmaf = predictor_target(req.target_vmaf, enc)
            cmd.extend([
                "--target-quality", f"{probe_vmaf:.1f}",
                "--probes", "5",
                # `--probe-slow` makes probe encodes use the same preset
                # as the final encode (instead of av1an's default fast
                # probe preset). Probes get slower but VMAF predictions
                # match the final encode's VMAF much more closely —
                # the dominant overshoot fix per av1an's docs.
                "--probe-slow",
            ])
            if getattr(enc, "av1an_min_q", None) is not None:
                cmd.extend(["--min-q", str(enc.av1an_min_q)])
            if getattr(enc, "av1an_max_q", None) is not None:
                cmd.extend(["--max-q", str(enc.av1an_max_q)])
        # 2-pass encoding for presets that need it.
        if getattr(enc, "av1an_passes", 1) > 1:
            cmd.extend(["--passes", str(enc.av1an_passes)])
        # Per-chunk pre-filter chain (denoise/sharpen for delivery presets).
        # av1an feeds chunks to ffmpeg; this `-vf` runs before the encoder
        # sees them, baking the pre-processing into every chunk.
        if enc.pre_filter:
            cmd.extend(["--ffmpeg", f"-vf {enc.pre_filter}"])
        return cmd

    async def encode(self, req: EncodeRequest, job: JobState) -> dict:
        from ..analysis import analyze
        ok, why = await self.is_available()
        if not ok:
            await job.emit(JobEvent(type="error", stage="error", message=why))
            raise RuntimeError(why)

        caps = get_capabilities()
        info = analyze(req.input_path)
        workers = int(req.extra_options.get("workers") or self._auto_workers())

        # av1an validates the demuxer-reported pixel format against the
        # encoder's accepted list BEFORE applying --pix-format. UYVY422 /
        # NV12 / RGB sources make svt-av1 abort with `does not support …`
        # in scene detection. We pre-transcode to a lossless yuv420p ffv1
        # mkv when needed and feed that to av1an.
        input_path = req.input_path
        preprocessed: Optional[Path] = None
        accepted = ENCODER_ACCEPTED_PIX_FMTS.get(req.encoder.av1an_encoder or "", set())
        src_pix_fmt = await _probe_pix_fmt(caps.ffprobe or "ffprobe", req.input_path)
        if src_pix_fmt and accepted and src_pix_fmt not in accepted:
            preprocessed = Path(req.output_path).with_name(
                f".av1an-pre-{Path(req.output_path).stem}.mkv"
            )
            await job.emit(JobEvent(
                type="log", stage="searching",
                message=(
                    f"Source pixel format {src_pix_fmt!r} is not accepted by "
                    f"{req.encoder.av1an_encoder}; pre-transcoding to yuv420p "
                    f"(ffv1 lossless)…"
                ),
            ))
            await job.emit(JobEvent(
                type="stage", stage="searching", percent=1,
                message=f"Pre-transcoding {src_pix_fmt} → yuv420p…",
            ))
            try:
                await _ffv1_to_yuv420p(
                    caps.ffmpeg or "ffmpeg", req.input_path, str(preprocessed),
                )
            except RuntimeError as e:
                preprocessed.unlink(missing_ok=True)
                await job.emit(JobEvent(type="error", stage="error", message=str(e)))
                raise
            input_path = str(preprocessed)
            await job.emit(JobEvent(
                type="log", stage="searching",
                message=f"Pre-transcode complete → {preprocessed.name}",
            ))

        cmd = self._build_command(req, workers, input_path)

        probe_vmaf = predictor_target(req.target_vmaf, req.encoder)
        target_msg = (
            f"target VMAF {req.target_vmaf:g}"
            if getattr(req.encoder, "fixed_crf", None) is not None
            else f"user target VMAF {req.target_vmaf:g}; probe target {probe_vmaf:.1f}"
        )
        await job.emit(JobEvent(
            type="log", stage="searching",
            message=(
                f"Launching av1an with encoder {req.encoder.av1an_encoder} "
                f"({workers} workers, {target_msg})"
            ),
            data={"command": " ".join(f'"{c}"' if " " in c else c for c in cmd), "workers": workers},
        ))
        await job.emit(JobEvent(
            type="stage", stage="searching", percent=2,
            message="Detecting scenes for parallel encoding…",
        ))

        # tool_env prepends the detected ffmpeg's dir to PATH — av1an shells
        # out to ffmpeg by name and would otherwise pick up the distro build.
        env = tool_env({
            "TERM": "dumb",  # try to suppress ANSI cursor games
            "RUST_LOG": os.environ.get("RUST_LOG", "info"),
        })

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(Path(req.output_path).parent),
                env=env,
            )
            tail_task = asyncio.create_task(
                self._tail_log_file(self._log_path, process, job, info.duration_s)
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
        finally:
            if preprocessed is not None:
                try:
                    preprocessed.unlink(missing_ok=True)
                except Exception:
                    pass
            try:
                tail_task.cancel()
            except Exception:
                pass
            try:
                self._log_path.unlink(missing_ok=True)
            except Exception:
                pass

    async def _tail_log_file(self, path: Path, process,
                              job: JobState, duration_s: float) -> None:
        """Tail av1an's debug log and emit structured 'metric' events.

        av1an writes scene info, per-chunk probes, target Q decisions, and
        per-chunk fps/duration to the log file with `--log-level debug`.
        These never appear on stdout when av1an is run non-interactively,
        so the UI would otherwise see ~4 lines for an entire encode.
        """
        # Wait for av1an to create the file; abort if process dies before.
        for _ in range(200):  # ~20 s max
            if path.exists():
                break
            if process.returncode is not None:
                return
            await asyncio.sleep(0.1)
        if not path.exists():
            return

        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                buf = ""
                while True:
                    chunk = fh.read()
                    if chunk:
                        buf += chunk
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            await self._handle_log_line(line.rstrip(), job)
                    else:
                        if process.returncode is not None:
                            # Drain whatever's left, then stop
                            if buf.strip():
                                await self._handle_log_line(buf.rstrip(), job)
                            return
                        await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _handle_log_line(self, line: str, job: JobState) -> None:
        """Parse a single log-file line and emit structured events for any
        recognised pattern. Falls back to a plain log forward for the rest."""
        if not line.strip():
            return

        # Echo the raw line at debug severity so it's visible if needed,
        # but tag it with `data.kind="av1an-debug"` so the UI can filter
        # it out by default (only show parsed events).
        forwarded = False

        m = RE_LOG_INPUT_INFO.search(line)
        if m:
            await job.emit(JobEvent(
                type="log", stage="searching",
                message=f"Input: {m.group(1)}×{m.group(2)} @ {m.group(3)} fps · {m.group(4)} · {m.group(5)}",
                data={"kind": "input_info", "width": int(m.group(1)),
                      "height": int(m.group(2)), "fps": float(m.group(3))},
            ))
            return

        m = RE_LOG_SCENE_INFO.search(line)
        if m:
            scenes = int(m.group(1))
            chunks = int(m.group(3))
            await job.emit(JobEvent(
                type="log", stage="searching",
                message=f"Scene detection: {scenes} scene(s), split into {chunks} chunk(s)",
                data={"kind": "scene_info", "scenes": scenes, "chunks": chunks,
                      "extra_split_frames": int(m.group(2))},
            ))
            return

        m = RE_LOG_TQ_PROBES.search(line)
        if m:
            chunk = int(m.group(1))
            # m.group(2) is e.g. "(86.12, 38), (85.04, 42)"
            probes = []
            for vmaf_s, q_s in re.findall(r"\(([\d.]+),\s*(\d+)\)", m.group(2)):
                probes.append({"vmaf": float(vmaf_s), "q": int(q_s)})
            note = (m.group(3) or "").strip()
            await job.emit(JobEvent(
                type="log", stage="searching",
                message=f"Chunk {chunk} probes: " + ", ".join(
                    f"q={p['q']}→{p['vmaf']:.1f}" for p in probes
                ) + (f"  ({note})" if note else ""),
                data={"kind": "tq_probes", "chunk": chunk, "probes": probes,
                      "note": note or None},
            ))
            return

        m = RE_LOG_TQ_TARGET.search(line)
        if m:
            chunk = int(m.group(1))
            q = int(m.group(2))
            vmaf = float(m.group(3))
            await job.emit(JobEvent(
                type="log", stage="searching",
                message=f"Chunk {chunk} → CRF {q} (predicted VMAF {vmaf:.2f})",
                data={"kind": "tq_target", "chunk": chunk, "q": q,
                      "predicted_vmaf": vmaf},
            ))
            return

        m = RE_LOG_CHUNK_START.search(line)
        if m:
            chunk = int(m.group(1))
            frames = int(m.group(2))
            await job.emit(JobEvent(
                type="log", stage="encoding",
                message=f"Chunk {chunk} encoding ({frames} frames)…",
                data={"kind": "chunk_start", "chunk": chunk, "frames": frames},
            ))
            return

        m = RE_LOG_CHUNK_DONE.search(line)
        if m:
            chunk = int(m.group(1))
            frames = int(m.group(2))
            fps = float(m.group(3))
            secs = float(m.group(4))
            await job.emit(JobEvent(
                type="log", stage="encoding",
                message=f"Chunk {chunk} done · {frames} frames · {fps:.1f} fps · {secs:.1f}s",
                data={"kind": "chunk_done", "chunk": chunk, "frames": frames,
                      "fps": fps, "seconds": secs},
            ))
            return

        if RE_LOG_PHASE_CONCAT.search(line):
            await job.emit(JobEvent(
                type="log", stage="encoded",
                message="Concatenating chunks with mkvmerge…",
                data={"kind": "phase", "phase": "concat"},
            ))
            return

        # Don't forward DEBUG/INFO context noise; only WARN/ERROR get through
        if "WARN " in line or "ERROR " in line or "panicked" in line.lower():
            await job.emit(JobEvent(
                type="log", stage="encoding", message=line,
                data={"kind": "warn"},
            ))

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
