"""Run ffmpeg with libvmaf / libvmaf_cuda and parse the JSON log.

The container's ffmpeg was built against a CUDA-enabled libvmaf, so the
`libvmaf_cuda` filter is registered. We pick CUDA when available unless
the caller forces CPU; both paths produce the same JSON schema.

Frame sync: the distorted clip is scaled to the reference's dimensions
before being fed into libvmaf. Mismatched frame counts are tolerated by
ffmpeg with `shortest=1` on the framesync (libvmaf inherits framesync).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


FFMPEG = "/usr/local/bin/ffmpeg"
FFPROBE = "/usr/local/bin/ffprobe"
DEFAULT_MODEL_DIR = Path(os.environ.get("VMAF_MODEL_DIR", "/usr/local/share/vmaf/model"))
DEFAULT_MODEL = "vmaf_v0.6.1"

log = logging.getLogger("vmaf.run")


@dataclass
class VMAFResult:
    distorted: str
    mean: float
    min: float
    max: float
    harmonic_mean: float
    frames: int          # frames libvmaf actually compared (= min of both)
    ref_frames: int      # frames in the reference input (probe)
    dist_frames: int     # frames in the distorted input (probe)
    target_w: int        # actual scale target fed to libvmaf
    target_h: int
    upscaled: bool       # True iff the 1080p upscale option kicked in
    seconds: float
    used_cuda: bool
    model: str
    log_path: Optional[str] = None


def cuda_available() -> bool:
    """Return True iff the libvmaf_cuda filter is present in this ffmpeg."""
    try:
        out = subprocess.run(
            [FFMPEG, "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=10,
        )
        return "libvmaf_cuda" in (out.stdout or "")
    except Exception:
        return False


def gpu_present() -> bool:
    """True iff nvidia-smi reports at least one GPU. Independent of the
    ffmpeg filter check — a CUDA-capable ffmpeg in a CPU-only container
    would still have the filter but fail at runtime."""
    if not shutil.which("nvidia-smi"):
        return False
    try:
        r = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0 and "GPU" in (r.stdout or "")
    except Exception:
        return False


async def _probe_video(path: str) -> dict:
    """Return {w, h, fps, nb_frames} for the first video stream.

    nb_frames is best-effort:
      1. container-reported `nb_frames` (instant, but missing in matroska
         from many encoders)
      2. fallback to `-count_packets` (decodes only headers — fast)
      3. 0 if even that fails (warned on at the call site)

    `-count_frames` is the only fully accurate option but it actually
    decodes every frame; way too slow for an interactive UI on long clips.
    For typical AVC/HEVC/AV1 streams, packets == frames.
    """
    proc = await asyncio.create_subprocess_exec(
        FFPROBE, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,nb_frames",
        "-of", "json", path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    data = json.loads(out.decode() or "{}")
    s = (data.get("streams") or [{}])[0]

    nb_frames = 0
    nb_str = str(s.get("nb_frames") or "")
    if nb_str.isdigit():
        nb_frames = int(nb_str)

    if nb_frames == 0:
        proc2 = await asyncio.create_subprocess_exec(
            FFPROBE, "-v", "error",
            "-select_streams", "v:0", "-count_packets",
            "-show_entries", "stream=nb_read_packets",
            "-of", "csv=p=0", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        pout, _ = await proc2.communicate()
        try:
            nb_frames = int(pout.decode().strip() or "0")
        except ValueError:
            nb_frames = 0

    return {
        "w": int(s.get("width") or 0),
        "h": int(s.get("height") or 0),
        "fps": str(s.get("avg_frame_rate") or ""),
        "nb_frames": nb_frames,
    }


def _model_path(model: str) -> str:
    """Resolve a model name to an absolute path. libvmaf accepts either
    `version=...` or `path=...`; we prefer path for clarity and so we can
    point at the bundled JSON copies even if libvmaf's lookup changes."""
    candidates = [
        DEFAULT_MODEL_DIR / f"{model}.json",
        DEFAULT_MODEL_DIR / model,
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return model  # fall back to libvmaf's built-in lookup by version


def _pick_target(ref_w: int, ref_h: int, upscale_1080p: bool) -> tuple[int, int, bool]:
    """Choose what resolution both inputs get scaled to before libvmaf.

    Default: the reference's own dims (libvmaf only requires both inputs
    match each other, not any specific size).

    When `upscale_1080p` is True AND the reference is below ab-av1's 1k
    threshold of 1728x972, both inputs are scaled to 1920x1080 instead.
    The libvmaf v0.6.1 model is trained on 1080p; SD content scored at
    its native resolution comes out roughly 15 VMAF points lower than the
    same content compared at 1080p, which is what ab-av1 reports.
    """
    if upscale_1080p and ref_w < 1728 and ref_h < 972:
        return 1920, 1080, True
    return ref_w, ref_h, False


def _build_cmd(
    *, reference: str, distorted: str, log_path: str,
    model: str, n_threads: int, use_cuda: bool,
    target_w: int, target_h: int,
) -> list[str]:
    """Construct the ffmpeg command. Both inputs are scaled to
    target_w x target_h before being fed into libvmaf."""
    model_arg = _model_path(model)
    # libvmaf option strings need ':' inside escaped from ffmpeg's filter
    # syntax — same gotcha as the path: prefix on Windows. We sidestep by
    # passing only basenames for log_path (caller runs us with cwd set to
    # the log's directory).
    log_basename = Path(log_path).name
    libvmaf_opts = (
        f"log_path={log_basename}"
        f":log_fmt=json"
        f":model=path={model_arg}"
        f":n_threads={n_threads}"
        f":pool=mean"
    )

    # Both inputs must reach libvmaf{,_cuda} in the SAME pixel format
    # (libvmaf rejects mixed; NVDEC outputs nv12 for 8-bit and p010le for
    # 10-bit content, and scale_cuda doesn't bridge p010le -> yuv420p).
    # We sidestep the format zoo by decoding on CPU and converting to
    # 8-bit yuv420p there — that's robust for any codec/bit-depth combo.
    # For the CUDA path, hwupload_cuda then hands the frames to
    # libvmaf_cuda, so the actual VMAF math still runs on the GPU
    # (which is the part that scales with content length anyway).
    #
    # `setpts=PTS-STARTPTS` zeros each input's first-frame timestamp so
    # libvmaf's framesync pairs frame-0 with frame-0 even if a container
    # reports a non-zero start_time (common with mkv/mp4 from av1an or
    # ffmpeg's mux delay). Without this, frame-0 of one input can pair
    # with frame-1 of the other and produce a per-frame VMAF of 0.
    sp = "setpts=PTS-STARTPTS"
    if use_cuda:
        return [
            FFMPEG, "-y", "-hide_banner", "-nostdin",
            "-loglevel", "info",
            "-i", reference,
            "-i", distorted,
            "-filter_complex",
            f"[0:v]{sp},scale={target_w}:{target_h}:flags=bicubic,format=yuv420p,"
            f"hwupload_cuda[ref];"
            f"[1:v]{sp},scale={target_w}:{target_h}:flags=bicubic,format=yuv420p,"
            f"hwupload_cuda[dist];"
            f"[dist][ref]libvmaf_cuda={libvmaf_opts}",
            "-an", "-f", "null", "-",
        ]
    else:
        return [
            FFMPEG, "-y", "-hide_banner", "-nostdin",
            "-loglevel", "info",
            "-i", reference,
            "-i", distorted,
            "-filter_complex",
            f"[0:v]{sp},scale={target_w}:{target_h}:flags=bicubic,format=yuv420p[ref];"
            f"[1:v]{sp},scale={target_w}:{target_h}:flags=bicubic,format=yuv420p[dist];"
            f"[dist][ref]libvmaf={libvmaf_opts}",
            "-an", "-f", "null", "-",
        ]


def _parse_log(log_path: Path) -> tuple[float, float, float, float, int]:
    """Read libvmaf JSON; return (mean, min, max, harmonic_mean, frames)."""
    data = json.loads(log_path.read_text())
    pooled = data.get("pooled_metrics", {}).get("vmaf", {})
    frames = data.get("frames") or []
    return (
        float(pooled.get("mean", 0.0)),
        float(pooled.get("min", 0.0)),
        float(pooled.get("max", 0.0)),
        float(pooled.get("harmonic_mean", 0.0)),
        len(frames),
    )


async def run_vmaf(
    *,
    reference: str,
    distorted: str,
    use_cuda: Optional[bool] = None,
    model: str = DEFAULT_MODEL,
    n_threads: int = 0,
    keep_log: bool = False,
    log_prefix: str = "",
    upscale_1080p: bool = False,
) -> VMAFResult:
    """Compute VMAF for a single (ref, dist) pair.

    use_cuda=None (default) → auto: CUDA if both filter and GPU are present.
    use_cuda=True → require CUDA; raise if filter or GPU is missing.
    use_cuda=False → force CPU.
    """
    tag = f"[{log_prefix}] " if log_prefix else ""
    ref_p = Path(reference)
    dist_p = Path(distorted)
    if not ref_p.is_file():
        raise FileNotFoundError(f"reference not found: {reference}")
    if not dist_p.is_file():
        raise FileNotFoundError(f"distorted not found: {distorted}")

    cuda_filter = cuda_available()
    gpu = gpu_present()
    if use_cuda is None:
        chosen_cuda = cuda_filter and gpu
    elif use_cuda:
        if not cuda_filter:
            raise RuntimeError("libvmaf_cuda filter not registered in ffmpeg")
        if not gpu:
            raise RuntimeError("no NVIDIA GPU available (nvidia-smi failed)")
        chosen_cuda = True
    else:
        chosen_cuda = False

    ref_info = await _probe_video(str(ref_p))
    dist_info = await _probe_video(str(dist_p))
    ref_w, ref_h = ref_info["w"], ref_info["h"]
    dist_w, dist_h = dist_info["w"], dist_info["h"]
    ref_frames = ref_info["nb_frames"]
    dist_frames = dist_info["nb_frames"]
    if ref_w <= 0 or ref_h <= 0:
        raise RuntimeError(f"could not probe reference dimensions: {reference}")

    target_w, target_h, did_upscale = _pick_target(ref_w, ref_h, upscale_1080p)
    log.info(
        "%sref %dx%d @%s · %s frames | dist %dx%d @%s · %s frames | "
        "target %dx%d%s | path=%s",
        tag, ref_w, ref_h, ref_info["fps"], ref_frames or "?",
        dist_w, dist_h, dist_info["fps"], dist_frames or "?",
        target_w, target_h,
        " (1080p upscale)" if did_upscale else "",
        "CUDA" if chosen_cuda else "CPU",
    )
    if ref_frames and dist_frames and ref_frames != dist_frames:
        log.warning(
            "%sframe-count mismatch: ref=%d dist=%d (Δ=%+d). libvmaf will "
            "trim to the shorter stream; per-frame scores may misalign if "
            "the extra frame is at the start. setpts=PTS-STARTPTS is on.",
            tag, ref_frames, dist_frames, dist_frames - ref_frames,
        )

    work_dir = Path(os.environ.get("VMAF_WORK_DIR", tempfile.gettempdir()))
    work_dir.mkdir(parents=True, exist_ok=True)

    # tempfile.NamedTemporaryFile puts the JSON next to other run artifacts
    # so the filter-chain log_path stays a bare basename (see _build_cmd).
    with tempfile.TemporaryDirectory(prefix="vmaf_", dir=str(work_dir)) as td:
        td_p = Path(td)
        log_path = td_p / "vmaf.json"
        cmd = _build_cmd(
            reference=str(ref_p), distorted=str(dist_p),
            log_path=str(log_path), model=model,
            n_threads=n_threads, use_cuda=chosen_cuda,
            target_w=target_w, target_h=target_h,
        )
        log.debug("%sffmpeg cmd: %s", tag, " ".join(shlex.quote(c) for c in cmd))

        import time
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(td_p),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_b = await proc.communicate()
        elapsed = time.monotonic() - t0

        if proc.returncode != 0:
            err = (stderr_b or b"").decode(errors="replace")[-2000:]
            # libvmaf_cuda hits assert(0) in CHECK_CUDA when PTX JIT fails
            # or the kernel can't initialise on the present GPU. When the
            # caller asked for auto (use_cuda=None), retry once on CPU
            # rather than failing the whole request.
            cuda_crashed = chosen_cuda and (
                "Aborted" in err
                or "Assertion" in err
                or "init_fex_cuda" in err
                or proc.returncode == -6  # SIGABRT
            )
            if cuda_crashed and use_cuda is None:
                log.warning(
                    "%sCUDA path crashed (rc=%s) — falling back to CPU. "
                    "stderr tail: %s",
                    tag, proc.returncode, err.strip().splitlines()[-1:] or [""],
                )
                chosen_cuda = False
                cmd = _build_cmd(
                    reference=str(ref_p), distorted=str(dist_p),
                    log_path=str(log_path), model=model,
                    n_threads=n_threads, use_cuda=False,
                    target_w=target_w, target_h=target_h,
                )
                t0 = time.monotonic()
                proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=str(td_p),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr_b = await proc.communicate()
                elapsed = time.monotonic() - t0
            if proc.returncode != 0:
                err = (stderr_b or b"").decode(errors="replace")[-2000:]
                log.error("%sffmpeg failed rc=%s. stderr tail:\n%s",
                          tag, proc.returncode, err)
                raise RuntimeError(
                    f"ffmpeg failed (rc={proc.returncode}). "
                    f"cmd: {' '.join(shlex.quote(c) for c in cmd)}\n--- stderr tail ---\n{err}"
                )

        if not log_path.exists():
            raise RuntimeError("ffmpeg succeeded but VMAF log was not produced")

        mean, vmin, vmax, hmean, frames = _parse_log(log_path)
        fps = (frames / elapsed) if elapsed > 0 else 0.0
        log.info(
            "%sffmpeg ok in %.2fs · %d frames · %.0f fps (%s)",
            tag, elapsed, frames, fps, "CUDA" if chosen_cuda else "CPU",
        )

        kept_path: Optional[str] = None
        if keep_log:
            keeper = work_dir / f"vmaf_{Path(distorted).stem}.json"
            keeper.write_text(log_path.read_text())
            kept_path = str(keeper)

    return VMAFResult(
        distorted=str(dist_p),
        mean=mean, min=vmin, max=vmax, harmonic_mean=hmean,
        frames=frames,
        ref_frames=ref_frames, dist_frames=dist_frames,
        target_w=target_w, target_h=target_h, upscaled=did_upscale,
        seconds=elapsed,
        used_cuda=chosen_cuda, model=model,
        log_path=kept_path,
    )


# ──────────────────────────────────────────────────────────────────────────
# Deep inspect: VMAF (regular + neg) + PSNR + SSIM in a single ffmpeg pass,
# plus a Laplacian-variance "detail retention" measure on a sampled frame.
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class RefBundle:
    """Reusable reference-side data so we can inspect N distorted clips
    against the same ref without re-probing or re-extracting the ref frame."""
    path: str
    w: int
    h: int
    frames: int
    codec: str
    bitrate_kbps: float
    target_w: int
    target_h: int
    upscaled: bool
    lap_var: float
    preview_b64: str
    midpoint_secs: float


@dataclass
class InspectScore:
    """Per-distorted metrics produced by one inspect call against a RefBundle."""
    distorted: str

    dist_codec: str
    dist_bitrate_kbps: float
    bitrate_ratio: float        # dist / ref (0.0 if ref_bitrate unknown)
    dist_w: int
    dist_h: int
    dist_frames: int
    frame_delta: int

    vmaf_mean: float
    vmaf_min: float
    vmaf_neg_mean: float
    vmaf_neg_min: float
    enhancement_gap: float
    psnr_y_db: float
    ssim_y: float

    lap_var_dist: float
    detail_retention: float

    seconds: float
    dist_preview_b64: str


@dataclass
class InspectResult:
    """Container carrying ref-once data + 1-or-2 per-distorted scores."""
    reference: str
    used_cuda: bool

    ref_codec: str
    ref_bitrate_kbps: float
    ref_w: int
    ref_h: int
    ref_frames: int
    target_w: int
    target_h: int
    upscaled: bool
    lap_var_ref: float
    ref_preview_b64: str

    scores: list[InspectScore]
    seconds: float


async def _probe_format(path: str) -> dict:
    """Bitrate + format info via ffprobe."""
    proc = await asyncio.create_subprocess_exec(
        FFPROBE, "-v", "error",
        "-show_entries", "format=bit_rate,duration : stream=codec_name,bit_rate",
        "-select_streams", "v:0", "-of", "json", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    data = json.loads(out.decode() or "{}")
    s = (data.get("streams") or [{}])[0]
    f = data.get("format") or {}
    # Stream bitrate is more accurate when present, but containers like mkv
    # don't always report it; fall back to the format-level number.
    br = s.get("bit_rate") or f.get("bit_rate") or "0"
    try:
        br_kbps = float(br) / 1000.0
    except ValueError:
        br_kbps = 0.0
    return {
        "codec": str(s.get("codec_name") or "?"),
        "bitrate_kbps": br_kbps,
    }


async def _extract_frame_png(path: str, t: float, out_png: Path) -> None:
    """Extract a single PNG frame at time `t` (seconds)."""
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{t:.3f}", "-i", path,
        "-frames:v", "1", "-update", "1",
        str(out_png),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0 or not out_png.exists():
        raise RuntimeError(
            f"frame extract failed: {(err or b'').decode(errors='replace')[-400:]}"
        )


def _laplacian_variance(png_path: Path) -> float:
    """High-frequency content proxy. Higher = more fine detail."""
    from PIL import Image
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view

    K = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float32)
    im = np.asarray(Image.open(png_path).convert("L"), dtype=np.float32)
    win = sliding_window_view(im, (3, 3))
    return float((win * K).sum(axis=(-1, -2)).var())


def _png_to_b64(png_path: Path, max_w: int = 480) -> str:
    """Read a PNG, downscale to <=max_w wide, return data: URL."""
    import base64
    from PIL import Image
    from io import BytesIO

    im = Image.open(png_path)
    if im.width > max_w:
        ratio = max_w / im.width
        im = im.resize((max_w, int(im.height * ratio)), Image.LANCZOS)
    buf = BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


async def _run_libvmaf(
    *, reference: str, distorted: str, log_path: Path,
    target_w: int, target_h: int, use_cuda: bool, n_threads: int,
    model_path: str, extra_features: str = "",
) -> tuple[int, str]:
    """One ffmpeg run with libvmaf{,_cuda}. Returns (returncode, stderr_tail)."""
    sp = "setpts=PTS-STARTPTS"
    opts = (
        f"log_path={log_path.name}"
        f":log_fmt=json"
        f":model=path={model_path}"
        f":n_threads={n_threads}"
        f":pool=mean"
    )
    if extra_features:
        opts += f":{extra_features}"

    if use_cuda:
        flt = (
            f"[0:v]{sp},scale={target_w}:{target_h}:flags=bicubic,"
            f"format=yuv420p,hwupload_cuda[ref];"
            f"[1:v]{sp},scale={target_w}:{target_h}:flags=bicubic,"
            f"format=yuv420p,hwupload_cuda[dist];"
            f"[dist][ref]libvmaf_cuda={opts}"
        )
    else:
        flt = (
            f"[0:v]{sp},scale={target_w}:{target_h}:flags=bicubic,"
            f"format=yuv420p[ref];"
            f"[1:v]{sp},scale={target_w}:{target_h}:flags=bicubic,"
            f"format=yuv420p[dist];"
            f"[dist][ref]libvmaf={opts}"
        )

    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", reference, "-i", distorted,
        "-filter_complex", flt,
        "-an", "-f", "null", "-",
        cwd=str(log_path.parent),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    return proc.returncode, (err or b"").decode(errors="replace")[-1500:]


async def _prepare_ref(
    *, reference: str, upscale_1080p: bool, work_dir: Path, td_p: Path,
    log_tag: str,
) -> RefBundle:
    """Probe reference, extract its midpoint frame, compute lap-var, encode b64."""
    ref_p = Path(reference)
    if not ref_p.is_file():
        raise FileNotFoundError(f"reference not found: {reference}")

    info = await _probe_video(str(ref_p))
    fmt = await _probe_format(str(ref_p))
    if info["w"] <= 0 or info["h"] <= 0:
        raise RuntimeError(f"could not probe reference dimensions: {reference}")

    target_w, target_h, did_upscale = _pick_target(
        info["w"], info["h"], upscale_1080p,
    )

    # Use frame 60 (~ 1 second in) — robust to the first GOP being a fade-in.
    fps = 60.0  # rough default; the exact value doesn't matter for mid sampling
    ref_secs = (info["nb_frames"] / fps) if info["nb_frames"] else 1.0
    midpoint = max(0.5, ref_secs / 2.0)

    ref_png = td_p / "ref.png"
    await _extract_frame_png(str(ref_p), midpoint, ref_png)
    try:
        lap_var = _laplacian_variance(ref_png)
    except Exception as e:
        log.warning("%sref Laplacian probe failed: %s", log_tag, e)
        lap_var = 0.0
    ref_b64 = _png_to_b64(ref_png)

    return RefBundle(
        path=str(ref_p),
        w=info["w"], h=info["h"], frames=info["nb_frames"],
        codec=fmt["codec"], bitrate_kbps=fmt["bitrate_kbps"],
        target_w=target_w, target_h=target_h, upscaled=did_upscale,
        lap_var=lap_var, preview_b64=ref_b64,
        midpoint_secs=midpoint,
    )


async def _inspect_one(
    *, ref: RefBundle, distorted: str, td_p: Path,
    use_cuda: bool, n_threads: int, log_tag: str,
) -> InspectScore:
    """Run the two libvmaf passes + frame extract for one distorted clip."""
    import time as _time
    dist_p = Path(distorted)
    if not dist_p.is_file():
        raise FileNotFoundError(f"distorted not found: {distorted}")

    info = await _probe_video(str(dist_p))
    fmt = await _probe_format(str(dist_p))
    dist_w, dist_h = info["w"], info["h"]
    dist_frames = info["nb_frames"]

    # libvmaf logs need bare basenames since cwd is set to td_p
    safe_stem = "".join(c for c in dist_p.stem if c.isalnum() or c in "._-")[:40] or "x"
    regular_log = td_p / f"vmaf_{safe_stem}.json"
    neg_log = td_p / f"vmafneg_{safe_stem}.json"
    dist_png = td_p / f"dist_{safe_stem}.png"

    t0 = _time.monotonic()

    rc, err = await _run_libvmaf(
        reference=ref.path, distorted=str(dist_p),
        log_path=regular_log,
        target_w=ref.target_w, target_h=ref.target_h,
        use_cuda=use_cuda, n_threads=n_threads,
        model_path=_model_path("vmaf_v0.6.1"),
        extra_features="feature=name=psnr|name=float_ssim",
    )
    if rc != 0:
        log.error("%sregular pass failed rc=%s tail:\n%s", log_tag, rc, err)
        raise RuntimeError(f"libvmaf (regular pass) failed: {err}")

    rc, err = await _run_libvmaf(
        reference=ref.path, distorted=str(dist_p),
        log_path=neg_log,
        target_w=ref.target_w, target_h=ref.target_h,
        use_cuda=use_cuda, n_threads=n_threads,
        model_path=_model_path("vmaf_v0.6.1neg"),
    )
    if rc != 0:
        log.error("%sneg pass failed rc=%s tail:\n%s", log_tag, rc, err)
        raise RuntimeError(f"libvmaf (neg pass) failed: {err}")

    reg = json.loads(regular_log.read_text())["pooled_metrics"]
    neg = json.loads(neg_log.read_text())["pooled_metrics"]
    v = reg["vmaf"]
    nv = neg["vmaf"]
    ssim_blob = reg.get("float_ssim") or reg.get("ssim") or {}
    psnr_blob = reg.get("psnr_y") or reg.get("psnr") or {}
    ssim_y = float(ssim_blob.get("mean") or 0.0)
    psnr_y = float(psnr_blob.get("mean") or 0.0)

    # Sample the same midpoint as the ref so we compare apples to apples.
    await _extract_frame_png(str(dist_p), ref.midpoint_secs, dist_png)
    try:
        lap_dist = _laplacian_variance(dist_png)
    except Exception as e:
        log.warning("%sdist Laplacian probe failed: %s", log_tag, e)
        lap_dist = 0.0
    dist_b64 = _png_to_b64(dist_png)

    elapsed = _time.monotonic() - t0
    detail_retention = (lap_dist / ref.lap_var) if ref.lap_var > 0 else 0.0
    bitrate_ratio = (
        (fmt["bitrate_kbps"] / ref.bitrate_kbps) if ref.bitrate_kbps > 0 else 0.0
    )
    enhancement_gap = float(v["mean"]) - float(nv["mean"])

    log.info(
        "%sscored %s · vmaf=%.2f neg=%.2f gap=%+.2f · psnr=%.2f ssim=%.4f · "
        "detail=%.0f%% · bitrate=%.0f kbps (%.2f%% of ref) · in %.2fs",
        log_tag, dist_p.name, v["mean"], nv["mean"], enhancement_gap,
        psnr_y, ssim_y, detail_retention * 100,
        fmt["bitrate_kbps"], bitrate_ratio * 100, elapsed,
    )

    return InspectScore(
        distorted=str(dist_p),
        dist_codec=fmt["codec"], dist_bitrate_kbps=fmt["bitrate_kbps"],
        bitrate_ratio=bitrate_ratio,
        dist_w=dist_w, dist_h=dist_h, dist_frames=dist_frames,
        frame_delta=(dist_frames - ref.frames),
        vmaf_mean=float(v["mean"]), vmaf_min=float(v["min"]),
        vmaf_neg_mean=float(nv["mean"]), vmaf_neg_min=float(nv["min"]),
        enhancement_gap=enhancement_gap,
        psnr_y_db=psnr_y, ssim_y=ssim_y,
        lap_var_dist=lap_dist, detail_retention=detail_retention,
        seconds=elapsed, dist_preview_b64=dist_b64,
    )


async def run_inspect(
    *,
    reference: str,
    distorted: list[str],
    use_cuda: Optional[bool] = None,
    upscale_1080p: bool = False,
    n_threads: int = 0,
    log_prefix: str = "",
) -> InspectResult:
    """Deep multi-metric inspection of 1 or 2 distorted clips against a ref.

    Two libvmaf passes per distorted (regular + neg) plus PSNR + SSIM features
    in the regular pass, plus a Laplacian-variance probe on the same midpoint
    frame from ref and each distorted. Cost ≈ 2× normal compare per distorted.
    """
    import time as _time

    if not distorted:
        raise ValueError("at least one distorted file required")
    if len(distorted) > 2:
        raise ValueError("at most two distorted files supported")

    tag = f"[{log_prefix}] " if log_prefix else ""

    cuda_filter = cuda_available()
    gpu = gpu_present()
    if use_cuda is None:
        chosen_cuda = cuda_filter and gpu
    elif use_cuda:
        if not cuda_filter:
            raise RuntimeError("libvmaf_cuda filter not registered in ffmpeg")
        if not gpu:
            raise RuntimeError("no NVIDIA GPU available (nvidia-smi failed)")
        chosen_cuda = True
    else:
        chosen_cuda = False

    work_dir = Path(os.environ.get("VMAF_WORK_DIR", tempfile.gettempdir()))
    work_dir.mkdir(parents=True, exist_ok=True)
    t0 = _time.monotonic()

    with tempfile.TemporaryDirectory(prefix="inspect_", dir=str(work_dir)) as td:
        td_p = Path(td)
        ref = await _prepare_ref(
            reference=reference, upscale_1080p=upscale_1080p,
            work_dir=work_dir, td_p=td_p, log_tag=tag,
        )
        log.info(
            "%sinspect start: %d distorted | target %dx%d %s | path=%s",
            tag, len(distorted), ref.target_w, ref.target_h,
            "(1080p↑)" if ref.upscaled else "",
            "CUDA" if chosen_cuda else "CPU",
        )

        scores: list[InspectScore] = []
        for i, d in enumerate(distorted):
            sub = f"{log_prefix}#{i + 1}" if log_prefix else f"#{i + 1}"
            scores.append(await _inspect_one(
                ref=ref, distorted=d, td_p=td_p,
                use_cuda=chosen_cuda, n_threads=n_threads,
                log_tag=f"[{sub}] ",
            ))

    elapsed = _time.monotonic() - t0
    log.info("%sinspect done in %.2fs total", tag, elapsed)

    return InspectResult(
        reference=ref.path,
        used_cuda=chosen_cuda,
        ref_codec=ref.codec, ref_bitrate_kbps=ref.bitrate_kbps,
        ref_w=ref.w, ref_h=ref.h, ref_frames=ref.frames,
        target_w=ref.target_w, target_h=ref.target_h, upscaled=ref.upscaled,
        lap_var_ref=ref.lap_var, ref_preview_b64=ref.preview_b64,
        scores=scores, seconds=elapsed,
    )
