# syntax=docker/dockerfile:1.6
#
# Multi-stage build:
#   1. ffmpeg-fetch  — pulls BtbN's static ffmpeg build (libvmaf, libsvtav1,
#                       x264/x265, libvpx, libopus, NVENC headers).
#   2. ab-av1-fetch  — pulls the latest ab-av1 Linux release (musl static).
#   3. av1an-build   — builds the Av1an backend from source: VapourSynth,
#                       SVT-AV1 v4, and av1an itself.
#   4. final         — slim Python image with the binaries + app code.
#
# NVENC: the static ffmpeg has the encoders compiled in. To use them at
# runtime the container needs the NVIDIA driver libs *with* the `video`
# capability. `--gpus all` alone gives only compute+utility, which makes
# av1_nvenc fail with "Cannot load libnvidia-encode.so.1". Use either:
#   docker run --gpus 'count=all,"capabilities=compute,utility,video"' ...
#   docker compose -f docker-compose.yml -f docker-compose.gpu.yml up
# The provided gpu compose file declares the right capabilities.
#
# The Av1an backend (scene-detected parallel encoding) is included by
# default. It adds VapourSynth + SVT-AV1 + a Rust build to the image — see
# stage 3. ab-av1 remains the default backend; av1an is the parallel option.

ARG VAPOURSYNTH_TAG=R72
ARG SVTAV1_TAG=v4.1.0

# ──────────────────────────────────────────────────────────────────────────
# Stage 1: ffmpeg + ffprobe (static, GPL build with libvmaf + libsvtav1 + NVENC)
# ──────────────────────────────────────────────────────────────────────────
FROM debian:12-slim AS ffmpeg-fetch
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl xz-utils ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt
ARG FFMPEG_URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz
RUN curl -fsSL --retry 3 -o ffmpeg.tar.xz "$FFMPEG_URL" \
 && mkdir -p ffmpeg \
 && tar -xf ffmpeg.tar.xz --strip-components=1 -C ffmpeg \
 && rm ffmpeg.tar.xz \
 && /opt/ffmpeg/bin/ffmpeg -hide_banner -filters | grep -q libvmaf \
 && /opt/ffmpeg/bin/ffmpeg -hide_banner -encoders | grep -qE '\blibsvtav1\b' \
 && /opt/ffmpeg/bin/ffmpeg -hide_banner -encoders | grep -qE '\bav1_nvenc\b' \
 && echo "ffmpeg static build verified"

# ──────────────────────────────────────────────────────────────────────────
# Stage 2: ab-av1 (latest release tarball)
# ──────────────────────────────────────────────────────────────────────────
FROM debian:12-slim AS ab-av1-fetch
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates zstd \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt
RUN set -eux \
 && url="$(curl -fsSL https://api.github.com/repos/alexheretic/ab-av1/releases/latest \
            | grep -oE 'https://[^"]+linux-musl[^"]*\.tar\.zst' \
            | head -1)" \
 && if [ -z "$url" ]; then echo "could not resolve ab-av1 download url" >&2; exit 1; fi \
 && echo "Downloading $url" \
 && curl -fsSL --retry 3 -o ab-av1.tar.zst "$url" \
 && zstd -d ab-av1.tar.zst -o ab-av1.tar \
 && mkdir extract \
 && tar -xf ab-av1.tar -C extract \
 && find extract -type f -name ab-av1 -exec install -m 0755 {} /opt/ab-av1 \; \
 && rm -rf extract ab-av1.tar.zst ab-av1.tar \
 && /opt/ab-av1 --version

# ──────────────────────────────────────────────────────────────────────────
# Stage 3: Av1an backend — VapourSynth + SVT-AV1 v4 + av1an, built from source
# ──────────────────────────────────────────────────────────────────────────
# Built on the full (non-slim) python image so the Python version matches the
# final stage exactly — VapourSynth's `vapoursynth.so` module and the Python
# that vsscript embeds must agree on ABI.
#
# VapourSynth is pinned to R72: the av1an `vapoursynth` Rust crate requests
# VSScript API 4.1, which R76+ rejects ("Failed to get VSScript API"). R72
# (API 4.2) accepts it. SVT-AV1 v4 is required by the libsvtav1* presets;
# apt's svt-av1 is v1.x. av1an 0.5.x needs rustc >= 1.88, hence rustup.
FROM python:3.13-bookworm AS av1an-build
ARG VAPOURSYNTH_TAG
ARG SVTAV1_TAG
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential autoconf automake libtool pkg-config nasm git cmake \
        clang curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Cython >= 3.1: older Cython generates code incompatible with modern
# CPython's PyLongObject internals.
RUN pip install --no-cache-dir 'Cython>=3.1'

# zimg — Debian bookworm ships 3.0.4; VapourSynth R72 requires >= 3.0.5.
RUN git clone --quiet --depth 1 -b release-3.0.5 --recurse-submodules \
        https://github.com/sekrit-twc/zimg /tmp/zimg \
 && cd /tmp/zimg \
 && ./autogen.sh && ./configure --disable-static --enable-shared \
 && make -j"$(nproc)" && make install && ldconfig \
 && cd / && rm -rf /tmp/zimg

# VapourSynth — autotools install into /usr/local. The python.org image uses
# site-packages (not Debian's dist-packages), so the module lands on sys.path.
RUN git clone --quiet --depth 1 -b "$VAPOURSYNTH_TAG" \
        https://github.com/vapoursynth/vapoursynth /tmp/vapoursynth \
 && cd /tmp/vapoursynth \
 && ./autogen.sh \
 && ./configure \
 && make -j"$(nproc)" \
 && make install \
 && ldconfig \
 && cd / && rm -rf /tmp/vapoursynth \
 && python3 -c 'import vapoursynth; print("VapourSynth", vapoursynth.core.version_number())'

# SVT-AV1 v4 — static SvtAv1EncApp; av1an drives it directly.
RUN git clone --quiet --depth 1 -b "$SVTAV1_TAG" \
        https://gitlab.com/AOMediaCodec/SVT-AV1.git /tmp/svt \
 && cd /tmp/svt \
 && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
          -DBUILD_SHARED_LIBS=OFF -DBUILD_TESTING=OFF -DBUILD_APPS=ON \
 && cmake --build build -j"$(nproc)" \
 && install -m 0755 Bin/Release/SvtAv1EncApp /usr/local/bin/SvtAv1EncApp \
 && cd / && rm -rf /tmp/svt \
 && SvtAv1EncApp --version

# av1an — via rustup (distro cargo is too old). PKG_CONFIG_PATH + -L so the
# `vapoursynth` crate links the source-built libs in /usr/local/lib.
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --default-toolchain stable --profile minimal \
 && PKG_CONFIG_PATH=/usr/local/lib/pkgconfig \
    RUSTFLAGS="-L /usr/local/lib" \
    /root/.cargo/bin/cargo install av1an --locked \
 && install -m 0755 /root/.cargo/bin/av1an /usr/local/bin/av1an \
 && rm -rf /root/.cargo /root/.rustup

# ──────────────────────────────────────────────────────────────────────────
# Stage 4: final runtime image
# ──────────────────────────────────────────────────────────────────────────
FROM python:3.13-slim-bookworm AS app

# ca-certificates for HTTPS, tini for clean PID-1 signal handling, and the
# Av1an runtime deps: x264/x265 (av1an drives them directly), mkvtoolnix
# (chunk concat), libffms2 (VapourSynth source plugin), libzimg2 (VapourSynth).
# zimg is built from source in stage 3 (Debian's is too old) and copied in
# below — so it is deliberately absent from this apt list.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates tini \
        x264 x265 mkvtoolnix libffms2-5 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Bake the binaries into /app/bin/ — app/tools.py:_find_ffmpeg checks here first.
COPY --from=ffmpeg-fetch  /opt/ffmpeg/bin/ffmpeg   /app/bin/ffmpeg
COPY --from=ffmpeg-fetch  /opt/ffmpeg/bin/ffprobe  /app/bin/ffprobe
COPY --from=ab-av1-fetch  /opt/ab-av1              /app/bin/ab-av1
RUN chmod 0755 /app/bin/*

# Av1an backend: VapourSynth libs + vspipe + the SVT-AV1 / av1an binaries,
# plus the VapourSynth Python module. Built against this exact Python version
# in stage 3, so it imports cleanly here.
COPY --from=av1an-build /usr/local/bin/vspipe        /usr/local/bin/vspipe
COPY --from=av1an-build /usr/local/bin/av1an         /usr/local/bin/av1an
COPY --from=av1an-build /usr/local/bin/SvtAv1EncApp  /usr/local/bin/SvtAv1EncApp
COPY --from=av1an-build /usr/local/lib/libvapoursynth.so* \
                        /usr/local/lib/libvapoursynth-script.so* \
                        /usr/local/lib/libzimg.so* \
                        /usr/local/lib/
COPY --from=av1an-build /usr/local/lib/python3.13/site-packages/vapoursynth.so \
                        /usr/local/lib/python3.13/site-packages/vapoursynth.so
# FFMS2 source plugin -> VapourSynth's autoload dir so the `ffms2` chunk
# method works, and refresh the linker cache for the VapourSynth libs.
RUN mkdir -p /usr/local/lib/vapoursynth \
 && ln -sf "$(ls /usr/lib/x86_64-linux-gnu/libffms2.so.* | head -1)" \
           /usr/local/lib/vapoursynth/libffms2.so \
 && ldconfig \
 && vspipe --version \
 && av1an --version

# Python deps (separated layer so app code edits don't bust the wheel cache)
COPY requirements.txt /app/
RUN python -m pip install --no-cache-dir --upgrade pip \
 && python -m pip install --no-cache-dir -r requirements.txt

# App code
COPY app/    /app/app/
COPY static/ /app/static/

# Runtime data dirs — also volume-mount points
RUN mkdir -p /app/uploads /app/outputs

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/app/bin:$PATH

EXPOSE 8000

# tini reaps zombie ffmpeg subprocesses cleanly
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/api/capabilities',timeout=3)" \
        || exit 1
