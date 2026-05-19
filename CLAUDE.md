# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Windows-targeted, browser-based video compression demo that takes any ffmpeg-readable video, analyzes its compressibility, recommends an output codec, and re-encodes it to hit a chosen VMAF target (85/90/95). The encoding work is delegated to **ab-av1** (default; supports NVENC) or **Av1an** (parallel/scene-detected; software encoders only). FastAPI backend + vanilla HTML/Tailwind frontend with SSE for live progress.

## Run

```powershell
# Windows (existing setup, ffmpeg already at C:\ffmpeg)
.\run.ps1                       # creates .venv, installs deps, starts uvicorn at http://127.0.0.1:8000
.\run.ps1 -Port 8765            # alternate port
```

```bash
# Linux (one-time)
./install.sh                    # ffmpeg + ab-av1 + Av1an backend + venv + deps
./install.sh --skip-av1an       # skip Av1an (no VapourSynth/SVT-AV1/Rust build)
# Then:
./run.sh                        # 127.0.0.1:8000
./run.sh --host 0.0.0.0 --port 8765
```

```bash
# Docker (multi-stage, ffmpeg + ab-av1 + Av1an baked in)
docker compose up --build                                                  # CPU only
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up --build  # NVENC
```

The main image builds the Av1an backend from source (VapourSynth R72 + SVT-AV1 v4 + av1an) in a dedicated build stage — see `Dockerfile` stage 3. `install.sh` does the same on the host by default; both can be skipped (`--skip-av1an` / not at all in Docker).

`install.sh` is idempotent and prefers a project-local static ffmpeg in `bin/` over whatever the distro shipped — distro ffmpeg packages frequently lack `libvmaf` or `libsvtav1`. `app/tools.py:_find_ffmpeg` checks `bin/` first, so the local copy always wins.

There are no tests, no linter, no build step. The frontend is served as static files; Tailwind is loaded via the Play CDN at runtime.

## External tools — required vs optional

The app **detects** these at startup (`app/tools.py`) and the frontend reflects what's available; nothing is hard-required to start, but encoding obviously needs ffmpeg + ab-av1.

| Tool | Where it's expected | Notes |
|---|---|---|
| `ffmpeg`, `ffprobe` | PATH or `C:\ffmpeg\bin\` | Must be built with `libvmaf`, `libsvtav1`, `libx264/265`, `libvpx`, NVENC |
| `ab-av1.exe` | PATH or `bin/ab-av1.exe` | **Auto-downloaded** from GitHub releases on first run if missing |
| `av1an` + VapourSynth R72 + L-SMASH/FFMS2 plugins | PATH | Optional. Av1an backend is greyed out in the UI when missing — don't try to "fix" that detection check; it's correct behaviour. |

## Architecture (the big picture)

The flow is **upload → analyze → user picks (backend, encoder, target VMAF) → encode → measure → compare**. The pieces:

- **`app/main.py`** — FastAPI routes + the encode driver `_run_encode_job`. Each encode runs as a fire-and-forget `asyncio.create_task`; the route returns a `job_id` immediately and the client subscribes to `/api/progress/{job_id}` over SSE. Two startup-time mutations live at the top of this file and matter:
  - `sys.stdout.reconfigure(encoding="utf-8")` — without this, any unicode in print output crashes on cp1252 Windows consoles.
  - `asyncio.set_event_loop_policy(WindowsProactorEventLoopPolicy())` — without this, `asyncio.create_subprocess_exec` raises `NotImplementedError` and every encode dies. **Don't move these below the FastAPI app construction.**

- **`app/progress.py`** — `JobState` exposes `subscribe()` which atomically registers a listener queue *and* returns a snapshot of history. The SSE handler in `main.py` relies on the no-await invariant between those two lines to avoid duplicate-emission races on reconnect. Don't add awaits inside `subscribe()`.

- **`app/analysis.py`** — Compressibility scoring is `bitrate / (W·H·fps)` (bits-per-pixel-per-frame), tiered against the source codec's efficiency class. The recommendation engine (`_recommend`) consults `caps.nvenc` to suggest hardware encoders only when the GPU actually has them. Tweak the bpp thresholds or the recommendation rules here, not in the frontend.

- **`app/encoders.py`** — Single source of truth for encoder metadata: container, sw/hw, which backends accept it, and **per-encoder `--min-crf` / `--max-crf` caps**. The CRF caps matter: NVENC quality drops off a cliff above ~CRF 36 and ab-av1's sample-based prediction extrapolates badly from there. If you add a new encoder, set sensible caps or VMAF targeting will overshoot.

- **`app/backends/`** — `ab_av1.py` and `av1an.py` both implement the `Backend` protocol from `base.py` and translate stdout into `JobEvent`s. Stage progression is `searching → encoding → measuring → done`. Both wrappers parse human-readable stdout via regex; **do not pass `--stdout-format json` to ab-av1's `auto-encode`** — that flag only exists on `sample-encode`.

- **`app/vmaf.py`** — Post-encode VMAF measurement. Two non-obvious things baked in:
  1. **Upscale sub-1080p input to 1080p before measurement.** The libvmaf 1k model is trained on 1080p; running it on 720p directly scores ~15 VMAF points lower than what ab-av1 reports for the same file. The `_video_dims` probe + scale chain mirrors ab-av1's `--vmaf-scale auto` rules. If you change this, expect predicted/achieved VMAF to diverge.
  2. **Run with `cwd=out_dir` and pass a bare filename for `log_path`.** Windows absolute paths contain `:` which collides with ffmpeg's filter argument separator (`D:/...` parses as a filter option). Switching to absolute paths or string-concatenated `log_path=` will silently break VMAF measurement on Windows.

- **`static/index.html` + `app.js`** — Single page, vanilla JS, no bundler. Tailwind utilities work because of the Play CDN script; **`@apply` only works inside `<style type="text/tailwindcss">` in `index.html`** (not in `style.css`). Custom theme colors `ink-*` and `accent-*` are extended in the inline `tailwind.config` block — keep them in sync if you add new shades.

## Where to extend

- **New encoder** → add an `Encoder(...)` entry to `CATALOG` in `app/encoders.py`. The frontend picks it up automatically. Set `backends=["ab-av1"]` only for NVENC entries; software encoders can include `"av1an"` and need an `av1an_encoder` + `av1an_params` mapping.
- **New backend** → implement `Backend` protocol (`is_available`, `encode`) in `app/backends/`, then add it to `_get_backend()` in `main.py` and the loop in `api_capabilities()`.
- **New compressibility heuristic** → edit `_classify_compressibility` and the matching `_expected_savings` table in `app/analysis.py`.
- **Frontend** → all logic is in `static/app.js`; `state` is a flat object near the top.

## Gotchas worth remembering

- ab-av1 prediction vs achieved VMAF can drift a few points on hardware encoders — the UI surfaces both numbers deliberately; don't "fix" the discrepancy by suppressing it.
- The `_uploads` and `registry` dicts are in-process memory only. Restarting uvicorn loses upload IDs and job history. For persistence, swap to sqlite — there's no migration to write since these are ephemeral demo state.
- `run.ps1`'s `--reload` is convenient for development but uvicorn's reloader re-imports `main.py`; the event-loop-policy and stdout-reconfigure block has to remain at module top so it runs on every reload.
- `bin/ab-av1.exe` is gitignored-by-convention (we auto-download). Don't commit it.
