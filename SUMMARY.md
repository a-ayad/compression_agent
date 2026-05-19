# Project journal

A running record of what we tried, what broke, and what we learned while
building out the compression agent + standalone CUDA VMAF service. Read
top-to-bottom to see the chronological investigation; jump to the
**Learnings** section for the distilled technical takeaways.

---

## Where things stand now

**Compression agent** (`/root/compression_agent`)
- FastAPI + vanilla JS frontend; SSE for live progress.
- Two backends: `ab-av1` (NVENC + sample-encode CRF probing) and `av1an`
  (chunked, scene-detected, parallel; software encoders only).
- Encoder catalog in `app/encoders.py` is the single source of truth.
- New encoder preset `libsvtav1-tiny` targets HandBrake-grade low-bitrate
  delivery (SVT-AV1 v4.1.0, preset 4, CRF 38–55 search, no pre-filter).
- Post-encode VMAF measurement now uses the same methodology as the
  deep-inspect service, with a +4 VMAF offset applied to backend probe
  targets to compensate for sample-based predictor inflation.

**VMAF service** (`/root/compression_agent/vmaf-service`)
- Standalone container. CUDA libvmaf when a GPU is present, CPU fallback.
- `/api/measure` (single distorted) and `/api/inspect` (1–2 distorted +
  cross-comparison findings).
- Per-frame VMAF (regular + neg model) + PSNR + SSIM + Laplacian-variance
  detail probe + base64 frame previews + structured findings/explanations.
- Optional 1080p upscale toggle.

**Docker images**
- `Dockerfile` (compression agent): unchanged baseline.
- `Dockerfile.av1an`: from-scratch SVT-AV1 v4.1.0 build + ffmpeg master
  built against it. Uses Kitware static cmake, Arch nasm, and
  nv-codec-headers from upstream tarball.
- `Dockerfile.vmaf`: CUDA-enabled ffmpeg + libvmaf, JSON-logged FastAPI.

---

## Trials & investigations

### 1. VMAF service log noise
**Want:** descriptive, structured per-job logs.
**Did:** added a JSON-logging filter that drops `/health` and
`/favicon.ico` polling, tagged every line with the job id, and surfaced
fps / duration / chunk events in the log payload.

### 2. av1an Docker image kept breaking
A series of cascading failures:

- **`libavutil.so.59` missing.** Caused by `pacman -Syu` upgrading the
  base ffmpeg from 7.x to 8.x mid-build. Fix: drop the system upgrade
  entirely; install only what we need (`nasm`) and use a static catatonit
  PID-1.
- **PEP 668 EXTERNALLY-MANAGED Python.** Arch's Python refuses pip
  installs without `--break-system-packages`. Fix: delete the marker
  before `python -m ensurepip`.
- **Pacman partial-upgrade cascade.** `pacman -Sy && pacman -S cmake`
  pulled an incompatible libarchive. Fix: download Kitware's static cmake
  binary; only pacman-install nasm.
- **CMake installer "/opt/cmake: No such file".** `--prefix=/opt/cmake`
  requires the dir to exist. Fix: `mkdir -p /opt/cmake` +
  `--exclude-subdir`.

### 3. av1an rejected `--target-metric`
The masterofzen base image's av1an predates that flag. Removed it; VMAF
is the default target metric anyway.

### 4. SVT-AV1 didn't accept UYVY422 input
av1an validates the demuxer-reported `pix_fmt` BEFORE applying
`--pix-format`. Anything outside the encoder's accepted set
(UYVY422 / NV12 / RGB) makes svt-av1 abort during scene detection.

**Fix in `app/backends/av1an.py`:** added `ENCODER_ACCEPTED_PIX_FMTS`
table per encoder, probe the source's `pix_fmt`, and pre-transcode to
lossless ffv1/yuv420p in mkv when needed. The intermediate is fed to
av1an and unlinked in the `finally` block.

### 5. VMAF service didn't surface frame-count or timestamp issues
**Did:** added timestamp realignment (`setpts=PTS-STARTPTS` on both
inputs), counted frames in reference vs distorted via ffprobe (with
`-count_packets` fallback), and emitted a warning when they disagree.

### 6. Sub-1080p VMAF was scoring ~15 points low
The libvmaf v0.6.1 1k model is *trained* on 1080p. SD content compared
at native resolution scores ~15 VMAF points lower than the same content
compared at 1080p — which is what ab-av1 reports during its probes.

**Did:** added `_pick_target()` in `vmaf-service/vmaf_runner.py` (and
mirrored the logic in `app/vmaf.py`) that upscales sub-1080p inputs to
1920×1080 to match ab-av1's `--vmaf-scale auto`. Made it a UI toggle on
the deep-inspect service.

### 7. Why does the "tiny" 600-kbps file have similar perceived quality to a 7.6-MB SVT-AV1 encode?
This was the rabbit hole that produced the deep-inspect feature.

Built `/api/inspect` which runs:
1. Regular VMAF (v0.6.1)
2. NEG-model VMAF (v0.6.1neg) — robust to enhancement filters that
   "cheat" the regular model
3. PSNR via libvmaf's `feature=name=psnr`
4. SSIM via `feature=name=float_ssim`
5. Laplacian variance at the midpoint frame as a detail-retention proxy

Then a rule-based findings engine combines those into prose
explanations: "enhancement gap" (regular − neg), "PSNR fidelity",
"detail retention" (Laplacian ratio), "SSIM corroboration", "bitrate
regime", "frame-count mismatch", and a composite verdict.

Verdict on the original tiny file: it was a **VMAF-tuned delivery
encode** — visible enhancement gap (regular VMAF outpaced the neg
model by ~5–6 points, the classic signature of unsharp masks gaming the
metric), low detail retention (~75%), and surprisingly high regular
VMAF for the bitrate.

### 8. Replicating the tiny file — first attempt overshot file size
**Tried:** added a `libsvtav1-tiny` encoder preset with
`pre_filter="unsharp=lx=3:ly=3:la=0.4"` and CRF probing.

**Result:** files came out *bigger* than the source (~8.7 Mbps).

**Why:** `unsharp` was *adding* detail (137% retention vs 100% baseline).
That extra detail also tricked ab-av1's VMAF probes into picking lower
CRF values, ballooning the bitrate. Removed the unsharp filter entirely.

### 9. Even without unsharp, output was way bigger than HandBrake's
Compared head-to-head against HandBrake's "Fast 1080p30 (Modified)"
preset — they got 453 kbps with great quality, we couldn't get under
~2 Mbps at the same VMAF target.

**HandBrake recipe (from their JSON config):**
- SVT-AV1 v4.1.0
- preset 5
- CRF 50
- GOP 320
- `enable-tf 1` (temporal filtering on)
- `--scd 1` (scene-cut detection on)
- `MultiPass: true` ← turned out to be a no-op for AV1

**Two real obstacles:**

1. **Our ffmpeg was linked against SVT-AV1 v2.1.2.** The system package
   shipped v2; v3+ changed the SONAME (.so.2 → .so.4) and the
   `svt_av1_enc_init_handle` signature. ffmpeg n7.1.1 wouldn't even
   compile against v4 (`enable_adaptive_quantization` was removed).

2. **HandBrake's `MultiPass: true` is a no-op for AV1.** SVT-AV1 v4
   explicitly rejects multi-pass with CRF: "CRF does not support
   multi-pass". We had been wasting a probe pass.

**Fix:**
- Built SVT-AV1 v4.1.0 from the GitLab tarball with `make install`.
- Switched ffmpeg to `master` branch (the libsvtav1 wrapper is updated
  there) and built against the new SVT.
- Added nv-codec-headers from upstream tarball for NVENC build deps.
- `PKG_CONFIG_PATH=/usr/local/lib/pkgconfig:...` to prefer v4 SVT over
  the stale system .pc files.
- Symlinked `/app/bin/ffmpeg` → `/usr/local/bin/ffmpeg`.
- Removed `--passes 2` from av1an params for SVT-AV1.

After the rebuild, the new pipeline produced **460 kbps at 38.82 dB
PSNR, 80% detail retention, +2.28 enhancement gap** — measurably *better*
than the original tiny file (75% retention, +5.57 gap) at comparable
bitrate.

### 10. av1an target-quality kept overshooting target VMAF
Even with the right SVT version, av1an's per-chunk target-quality
search consistently produced higher VMAF than asked.

**Fixes layered in:**
- `--probes 5` (default 4) — extra sample encode per chunk, tighter
  binary search.
- `--probe-slow` — probes use the same preset as the final encode
  instead of the default fast probe preset. This was the dominant
  overshoot fix per av1an's own docs.
- `av1an_min_q` / `av1an_max_q` per-encoder bounds. For the tiny
  preset, 38–55 makes av1an test the high-CRF (small-file) end first
  and only drop CRF when content needs it.
- `recommended_vmaf_target=85` on the tiny preset, with the UI
  auto-snapping the slider when the encoder is selected. (At an honest
  VMAF 90 you can't compress a 60fps action clip down to 500 kbps.)

### 11. UI: encoder log was useless for performance debugging
av1an piped to a non-tty emits ~4 stdout lines for an entire encode.
The rich progress info (TQ-Probes, target Q chosen, per-chunk fps,
durations, scene info) only appears in the log file.

**Did:**
- Pass `--log-file <stem>` + `--log-level debug` to av1an.
- New `_tail_log_file()` coroutine in `app/backends/av1an.py` that polls
  the file and streams new content.
- `_handle_log_line()` parses 7 patterns and emits structured
  `JobEvent`s with `data.kind` discriminator (`input_info`,
  `scene_info`, `tq_probes`, `tq_target`, `chunk_start`, `chunk_done`,
  `phase`, `warn`).
- Frontend: new `#encode-stats` panel with 4 KPI tiles (chunks done,
  current chunk fps, target Q, predicted VMAF) + per-chunk progress
  list + colored event spans by `kind`.

### 12. The big one: agent reported "VMAF 90" but deep-inspect said 65
On DJI 60fps footage:
- Compression agent's post-encode measurement: **VMAF 90**
- Standalone deep-inspect service: **VMAF 65** on the same file

A 25-point measurement gap is not a model-choice difference. Tracked
it down to `app/vmaf.py`:

```python
"-r", "25", "-i", distorted_path,
"-r", "25", "-i", reference_path,
```

The original docstring even admitted the goal: *"decode both inputs at
a fixed 25 fps. Without this alignment the same file will score
systematically lower than ab-av1's predictions."*

That's the bug stated as the design intent. The author had calibrated
the agent's measurement to **match the predictor**, not to be honest.
ab-av1's predictor runs sample-encode VMAF on short normalised chunks
which scores systematically high on motion content; baking that same
normalisation into the post-encode measurement pretended the inflation
wasn't there.

Three real divergences from the deep-inspect service:
1. `-r 25` framerate forcing (the headline issue)
2. No `format=yuv420p` — DJI 10-bit HEVC vs 8-bit SVT-AV1 output
   forced libvmaf to silently auto-convert one side
3. No explicit `model=version=...` — left to libvmaf's default

**Fix (the change you're reading the journal for):**
- `app/vmaf.py` rewritten to match `vmaf-service/vmaf_runner.py` exactly:
  native fps, `setpts=PTS-STARTPTS,scale=W:H:flags=bicubic,format=yuv420p`,
  explicit `model=version=vmaf_v0.6.1`. Scale target now picked from the
  *reference* (the source of truth for what dims matter).
- New shared `PREDICTOR_VMAF_OFFSET = 4.0` in `app/backends/base.py`
  with a `predictor_target(user_target, encoder)` helper.
- ab-av1 backend now passes `--min-vmaf {target+4}`.
- av1an backend now passes `--target-quality {target+4}`.
- Both backends log "user target VMAF X; probe target Y" so the
  compensation is visible.

Net effect: pick VMAF 90, backends aim for 94, honest post-encode
measurement should land near 90. Files that previously came back
labeled "VMAF 90" will now honestly read mid-80s and be slightly
larger to compensate.

### 13. `install.sh` reported "ffmpeg incomplete" on a build that was fine
After a clean Linux install the script kept re-downloading ffmpeg and
then dying with *"Installed ffmpeg still missing libvmaf/libsvtav1"*.
Two independent bugs stacked on top of each other:

**Bug A — johnvansickle's static ffmpeg has no libsvtav1.** The
johnvansickle.com build is widely used but ships a `--enable-` set
without SVT-AV1. The encoder check `grep '^V[. ]+libsvtav1'` was
therefore correctly failing; the diagnostic was honest, the source URL
was wrong.

**Fix:** switched `install_static_ffmpeg` to BtbN/FFmpeg-Builds GPL
release (`ffmpeg-master-latest-linux64-gpl.tar.xz`), which ships
libsvtav1 + libvmaf + NVENC. BtbN nests binaries one level deeper than
johnvansickle (`<dir>/bin/ffmpeg` instead of `<dir>/ffmpeg`), so the
extraction step now uses `find -type f -name ffmpeg` rather than the
hardcoded top-level path.

**Bug B — `grep -q` + `set -o pipefail` = false negatives on large
streams.** `have_full_ffmpeg` did:

```bash
"$ff" -hide_banner -encoders 2>/dev/null | grep -q libsvtav1
```

`grep -q` exits on the first match. With `set -o pipefail`, ffmpeg
then gets `SIGPIPE` (exit 141) when it tries to keep writing — and
the whole pipeline inherits that non-zero status. So the check
returned "not present" *even when the match had succeeded*. This
happened to work on johnvansickle's smaller `-filters` / `-encoders`
output (ffmpeg finished writing before grep closed the pipe) but
reliably failed on BtbN's longer output.

**Fix:** replaced `grep -q PATTERN` with `grep PATTERN >/dev/null`
everywhere in `install.sh`. grep now consumes the whole stream, ffmpeg
exits cleanly, pipefail is happy.

Also flipped the executable bit on `install.sh` and `run.sh` — they
were committed without `+x` on the original first commit and required
`bash ./install.sh` to run.

### 14. Is av1an still worth keeping? — and the ffmpeg-on-PATH bug
Prompted by a "does it still make sense to keep both backends" review.
av1an's only *exclusive* job is the `libsvtav1-tiny` delivery preset
(`backends=["av1an"]`); everything else lists av1an as a redundant
second path, and all three NVENC encoders are ab-av1-only.

The `encoders.py` comment justified `libsvtav1-tiny` being av1an-only
with: *"ffmpeg in this image links to SVT-AV1 v2.1.2; av1an invokes
SvtAv1EncApp directly to reach v4."* That is now stale — `bin/ffmpeg`
is the BtbN build and links **SVT-AV1 v4.1.0-7-gb486d839** (also
verified x265 `4.1+241`, x264 core 165 — all current).

**Test:** ran `ab-av1 auto-encode` with the tiny preset's exact
settings on a 10s vidyo4 talking-head clip.

- **First run failed** — `ffmpeg vmaf exit code 8 … No such filter:
  'libvmaf'`. ab-av1 had shelled out to the *distro* ffmpeg (Ubuntu
  6.1.1, no libvmaf), not `bin/ffmpeg`.
- **Root cause:** ab-av1 has no `--ffmpeg` flag — it resolves `ffmpeg`
  from `PATH` — and `app/backends/ab_av1.py` spawned it with no `env=`
  override. `_find_ffmpeg()`'s preference for `bin/` never reached the
  child. This silently affected *all* ab-av1 encoding on Linux, not
  just the tiny preset.
- **Re-run with `bin/` on PATH:** binary search settled CRF 49.25 →
  predicted VMAF 89.06, output **586 KB / ~479 kbps** — squarely in
  the same delivery regime as av1an's existing tiny outputs on the
  same source (337 / 434 / 460 kbps). av1an is no longer *technically*
  required for this preset.

**Fix #1 (the PATH bug):**
- New `tool_env(extra=None)` in `app/tools.py` — copies the environment
  and prepends the detected ffmpeg's directory to `PATH`.
- `ab_av1.py` and `av1an.py` now spawn their subprocesses with
  `env=tool_env(...)`. The probe/pre-transcode calls in `av1an.py`
  already used explicit full paths, so only the tools that shell out
  to `ffmpeg` *by name* (ab-av1, av1an itself) needed it.
- Rewrote the stale `encoders.py` comment: records that the v2.1.2
  rationale is dead, that ab-av1 can now do the preset, and the PATH
  caveat that must be fixed before flipping `backends`.

Status: `libsvtav1-tiny` still lists `backends=["av1an"]` — moving it
to ab-av1 is a follow-up, now unblocked by Fix #1.

### 15. Installing the av1an backend on Ubuntu 24.04
`./install.sh --with-av1an` was a silent no-op on noble:

```
E: Unable to locate package vapoursynth
E: Unable to locate package python3-vapoursynth
E: Unable to locate package ffms2
cargo not found → Av1an step skipped
```

Ubuntu 24.04 dropped the `vapoursynth` package entirely, never had an
`av1an` package, and `ffms2` is now `libffms2-dev`. The whole apt path
was dead. Did a from-source install instead.

**VapourSynth version matters — R76 breaks av1an.** Built the latest
release (R76) first; av1an then panicked at startup:

```
panicked at vapoursynth-0.5.1/src/vsscript/mod.rs:83:
Failed to get VSScript API
```

`getVSScriptAPI()` returned null — confirmed with a bare C call, so it
was VapourSynth, not the Rust crate. Root cause: the av1an `vapoursynth`
crate (`vapoursynth-sys 0.5.0`) requests **VSScript API 4.1**; R76
provides 4.3 and its `getVSScriptAPI` *rejects* the older 4.1 request
(R76 broke backward-compat). Checked the tags:

| Release | VSScript API minor | `getVSScriptAPI(4.1)` |
|---|---|---|
| R65, R68 | 1 | ok |
| R70, R72 | 2 | ok |
| R76 | 3 | **null** |

Rebuilt with **R72** (VSScript API 4.2 — also the version this
project's docs reference). `getVSScriptAPI(4.1)` then returns a valid
pointer and av1an runs.

**Other gotchas hit along the way:**
- *Cython.* Ubuntu's Cython 3.0.8 generates code that fails against
  Python 3.12's `PyLongObject` internals (`PyLong_SHIFT` undeclared).
  Needs Cython ≥ 3.1 — `pip install -U Cython`.
- *Build systems differ by tag.* R76 uses meson + a `mesonpy`
  pip backend that installs everything as a Python package. R72 uses
  autotools — traditional `/usr/local` layout, which is what av1an
  expects anyway.
- *Debian python path.* R72's autotools `make install` drops the
  Python module in `/usr/local/lib/pythonX.Y/site-packages`; Debian
  Python only searches `dist-packages`. Copy it across or `import
  vapoursynth` fails → vsscript can't init → `vspipe` prints "Failed
  to initialize VSScript".
- *Linker vs loader.* av1an's `vapoursynth-sys` emits bare
  `-lvapoursynth -lvapoursynth-script` with no search path. `ldconfig`
  fixes runtime lookup but not link time — the build needs
  `-L /usr/local/lib` (or pkg-config with a proper `Libs:` line).
- *Rust version.* av1an 0.5.2 needs rustc ≥ 1.88; Ubuntu's cargo is
  1.75. Installed rustup (got 1.95).
- *FFMS2 plugin.* apt's `libffms2.so` exports `VapourSynthPluginInit2`
  — it *is* a valid VapourSynth source plugin. Symlink it into
  VapourSynth's autoload dir (`/usr/local/lib/vapoursynth`) and the
  `ffms2` chunk method works.
- *Encoders.* av1an drives encoder *binaries* directly, not ffmpeg.
  x264/x265 come from apt. `SvtAv1EncApp` does **not** — apt's
  `svt-av1` is v1.7, far too old for the libsvtav1* presets (need v4).
  Until SVT-AV1 v4 is built from source, av1an can do x264/x265 but
  not the AV1 presets; ab-av1 covers AV1 (ffmpeg's SVT-AV1 v4.1.0).

Smoke-tested: av1an end-to-end (scene detect → FFMS2 chunking → parallel
x265 → mkvmerge concat) succeeds. `install.sh` rewritten with an
apt-aware path that builds VapourSynth R72 from source and installs
av1an via rustup/cargo.

---

## Learnings

### VMAF measurement
- **Forcing input framerate (`-r N` before `-i`) inflates motion-content
  VMAF.** Don't do it for honest measurement. The "match ab-av1's
  predictions" excuse means matching a predictor that is itself
  optimistic on samples.
- **Always normalise pixel format before libvmaf.** 10-bit HEVC vs
  8-bit AV1 output without explicit `format=yuv420p` produces silent
  auto-conversion that affects scores.
- **Pin the model explicitly** (`model=version=vmaf_v0.6.1` or
  `model=path=...`). Default lookup can drift across libvmaf versions.
- **`setpts=PTS-STARTPTS` is non-optional.** mkv/mp4 from av1an or
  ffmpeg mux often have non-zero start_time; without resetting, libvmaf's
  framesync can pair frame 0 of one input with frame 1 of the other and
  silently produce per-frame VMAF of 0.
- **Sub-1080p content needs upscaling to 1080p** for honest scoring with
  the 1k model — ~15 VMAF points difference.
- **Sample-based VMAF predictors (ab-av1, av1an target-quality) score
  ~2–7 points higher than full-clip honest measurement** on motion
  content. Compensate at the target, not at the measurement.

### SVT-AV1
- **v3 broke ABI vs v2** (SONAME .so.2 → .so.4). Distro ffmpeg packages
  on long-lived bases will silently link against the old SVT and miss
  v4's compression gains. Build from source for v4.
- **`enable_adaptive_quantization` was removed in v3.** ffmpeg n7.x
  won't compile against v4 — need master.
- **Multi-pass is a no-op with CRF** in v4 (the encoder rejects it).
  HandBrake's `MultiPass: true` does nothing for AV1.
- **av1an talks to SvtAv1EncApp directly**, ffmpeg+libsvtav1 doesn't.
  Keeping av1an's PATH `/usr/local/bin` first picks up the v4 binary
  even when the system ffmpeg is linked to v2.

### av1an
- **Default probe preset is fast.** Use `--probe-slow` so probe VMAF
  matches final-encode VMAF.
- **Bound the search range** with `--min-q`/`--max-q` to bias toward an
  operating point. min=38/max=55 makes the binary search test the
  high-CRF end first — perfect for delivery presets.
- **`--target-metric vmaf` doesn't exist on older av1an builds.** VMAF
  is the default; don't pass it.
- **Source `pix_fmt` is validated before scene detection.** UYVY422 /
  NV12 / RGB inputs need a pre-transcode to a yuv420p ffv1 mkv
  intermediate or the encoder aborts.
- **`--log-file <stem>` always appends `.log`.** Tail
  `<stem>.log`, not `<stem>`.
- **`--log-level debug` is required to expose per-chunk TQ-Probes,
  target Q, fps, duration.** Stdout in non-tty mode shows almost
  nothing.

### ab-av1
- **`--stdout-format json` only exists on `sample-encode`,** not on
  `auto-encode`. Don't pass it to auto-encode.
- **NVENC quality drops fast above CRF ~36.** Sample-based prediction
  extrapolates badly from there. Set `--max-crf 36` for NVENC encoders
  in the catalog.
- **ab-av1 has no `--ffmpeg` flag — it resolves `ffmpeg` from `PATH`.**
  Detecting a preferred ffmpeg (e.g. a static `bin/` build) is useless
  unless that directory is on the child's `PATH`. Spawn ab-av1 with an
  env whose `PATH` is prepended, or it silently uses the distro ffmpeg
  — which may link an old SVT-AV1 and lack the libvmaf filter (VMAF
  probes then die with `No such filter: 'libvmaf'`).
- **The same applies to av1an** — it shells out to `ffmpeg` by name
  too. Anything that detects a binary by full path must still export
  that path to subprocesses that look it up by name.

### libvmaf gotchas
- **Windows absolute paths break libvmaf filter args** — the `:` in
  `D:/...` parses as a filter option separator. Use `cwd=out_dir` and
  pass a bare filename for `log_path=`.
- **CUDA libvmaf rejects mixed pixel formats** between the two inputs.
  Do CPU-side `format=yuv420p` *before* `hwupload_cuda` for both
  inputs; the actual VMAF math still runs on the GPU after that.

### Shell scripting under `set -o pipefail`
- **`grep -q` is unsafe in a pipeline under `pipefail`.** It exits on
  first match, SIGPIPEs the upstream producer (exit 141), and the
  pipeline status becomes failure even though the *grep* succeeded.
  Use `grep PATTERN >/dev/null` (or `grep -m1 PATTERN >/dev/null` if
  you do want early termination — but consume the SIGPIPE outcome
  deliberately).
- **`set -o pipefail` interacts with every short-circuiting pipe stage,
  not just the obvious ones.** `head -1`, `grep -q`, `tee` with a
  failing reader — any consumer that closes early can flip the
  pipeline's exit status to non-zero. Audit pipelines after enabling
  pipefail.
- **Commit executable bits explicitly.** Files added to git without
  `+x` keep mode `0644` forever; `git update-index --chmod=+x` (or a
  fresh `chmod +x` followed by `git add`) is the only fix. Symptoms
  look like "this script won't run" until someone tries `bash ./x.sh`
  and it works.

### Static ffmpeg builds
- **johnvansickle's static ffmpeg lacks libsvtav1.** It has libvmaf
  but not the SVT-AV1 encoder. Fine for analysis-only tools, broken
  for anything that needs to *produce* AV1. Use BtbN/FFmpeg-Builds GPL
  release instead — it ships libsvtav1 + libvmaf + NVENC headers.
- **BtbN nests binaries under `<extracted-dir>/bin/`.** Don't hardcode
  the johnvansickle top-level layout when switching sources; `find
  -type f -name ffmpeg` survives both.
- **Validate static builds with a feature probe, not a version check.**
  `ffmpeg -encoders | grep libsvtav1` is the truth; `ffmpeg -version`
  tells you nothing about which encoders made it into the build.

### VapourSynth / av1an install
- **VapourSynth is no longer an apt package on Ubuntu 24.04.** Build
  from source. dnf/pacman still ship it.
- **Pin VapourSynth to R72 for av1an.** The av1an `vapoursynth` crate
  requests VSScript API 4.1; R76+ rejects it (`getVSScriptAPI` returns
  null → "Failed to get VSScript API" panic). R70–R72 (API minor 2)
  accept it.
- **Diagnose `getVSScriptAPI` failures with a bare C call.** It isolates
  VapourSynth from the Rust binding instantly — link
  `-lvapoursynth-script`, call `getVSScriptAPI(VS_MAKE_VERSION(4,1))`.
- **Cython 3.0.x can't target Python 3.12** (`PyLong_SHIFT` undeclared
  in generated code). Use Cython ≥ 3.1.
- **autotools + Debian Python = wrong install dir.** `make install`
  uses `site-packages`; Debian searches `dist-packages`. Copy the
  module across.
- **`ldconfig` fixes runtime linking, not link-time.** The build-time
  linker only searches standard dirs + explicit `-L`. A lib in
  `/usr/local/lib` still needs `-L /usr/local/lib` (or pkg-config) to
  *link* against, even though ldconfig will *load* it fine.
- **av1an drives encoder binaries, not ffmpeg.** It needs `x264`,
  `x265`, `SvtAv1EncApp` etc. on PATH. apt's `svt-av1` (v1.x) is too
  old for the libsvtav1* presets — build SVT-AV1 v4 from source.
- **apt's `libffms2.so` is a valid VapourSynth plugin** (exports
  `VapourSynthPluginInit2`) — no separate VS-plugin package needed.

### Docker / Arch
- **Never `pacman -Syu` mid-build.** It will pull an ffmpeg major
  upgrade that invalidates any prior layer's `.so` linking.
- **Partial upgrades break libarchive.** `pacman -Sy && pacman -S X`
  is unsafe; either commit to a full upgrade or use static binaries.
- **Kitware's static cmake binary** sidesteps the libarchive cascade
  entirely. Pin a version, mkdir the prefix first, use
  `--exclude-subdir`.
- **PEP 668 EXTERNALLY-MANAGED marker** has to be removed before
  `python -m ensurepip` will let you install anything.

### UI / DX
- **An encoder that auto-snaps the VMAF target slider** is the right
  way to express "this preset has a different design point". Users
  don't read tooltips; they pick a slider value and click go.
- **Structured `JobEvent` events with a `kind` discriminator** beat
  free-form log messages. Frontend can color them, filter them,
  promote them into KPI tiles, etc.
- **Show the user's target *and* the internal probe target.** When
  they disagree (because of the +4 compensation), users want to
  understand why their probe logs say "94" instead of "90".

### Memory / process
- **Don't trust a docstring that admits its own hack.** "Without this
  alignment the same file will score systematically lower than
  ab-av1's predictions" was a confession, not a justification. Read
  hack-acknowledging comments as bug reports.
- **A measurement that agrees with the predictor is not a vindicated
  measurement.** It's a measurement calibrated to the predictor.
  Validate against a fully independent path (the deep-inspect service)
  before trusting either.
