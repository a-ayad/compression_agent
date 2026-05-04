# syntax=docker/dockerfile:1.6
#
# Multi-stage build:
#   1. ffmpeg-fetch  — pulls BtbN's static ffmpeg build (libvmaf, libsvtav1,
#                       x264/x265, libvpx, libopus, NVENC headers).
#   2. ab-av1-fetch  — pulls the latest ab-av1 Linux release (musl static).
#   3. final         — slim Python image with the binaries + app code.
#
# NVENC: the static ffmpeg has the encoders compiled in. To use them at
# runtime the container needs the NVIDIA driver libs *with* the `video`
# capability. `--gpus all` alone gives only compute+utility, which makes
# av1_nvenc fail with "Cannot load libnvidia-encode.so.1". Use either:
#   docker run --gpus 'count=all,"capabilities=compute,utility,video"' ...
#   docker compose -f docker-compose.yml -f docker-compose.gpu.yml up
# The provided gpu compose file declares the right capabilities.
#
# Av1an backend is NOT included — it requires VapourSynth + plugins which
# bloat the image significantly. Use the host install.sh --with-av1an path
# if you need scene-detected parallel encoding.

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
# Stage 3: final runtime image
# ──────────────────────────────────────────────────────────────────────────
FROM python:3.13-slim-bookworm AS app

# ca-certificates for HTTPS, tini for clean PID-1 signal handling
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Bake the binaries into /app/bin/ — app/tools.py:_find_ffmpeg checks here first.
COPY --from=ffmpeg-fetch  /opt/ffmpeg/bin/ffmpeg   /app/bin/ffmpeg
COPY --from=ffmpeg-fetch  /opt/ffmpeg/bin/ffprobe  /app/bin/ffprobe
COPY --from=ab-av1-fetch  /opt/ab-av1              /app/bin/ab-av1
RUN chmod 0755 /app/bin/*

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
