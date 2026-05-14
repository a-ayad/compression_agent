#!/usr/bin/env bash
# install.sh — one-shot setup for the video-compression-agent on Linux.
#
# Drops a static ffmpeg build (with libvmaf + libsvtav1 + NVENC) and the
# ab-av1 binary into ./bin/, creates a Python venv, installs deps, and
# optionally installs Av1an + VapourSynth. Re-run safely: every step is
# idempotent.
#
# Usage:
#   ./install.sh                      # baseline: ffmpeg + ab-av1 + venv
#   ./install.sh --with-av1an         # also install Av1an (parallel backend)
#   ./install.sh --skip-ffmpeg        # use system ffmpeg (must have libvmaf)
#   ./install.sh --python python3.11  # pin a specific interpreter
#   ./install.sh --help

set -euo pipefail

# ── Args ───────────────────────────────────────────────────────────────────
WITH_AV1AN=0
SKIP_FFMPEG=0
PYTHON_BIN="${PYTHON:-python3}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-av1an)   WITH_AV1AN=1; shift ;;
        --skip-ffmpeg)  SKIP_FFMPEG=1; shift ;;
        --python)       PYTHON_BIN="$2"; shift 2 ;;
        -h|--help)
            sed -n '1,15p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

ROOT="$(cd "$(dirname "$0")" && pwd)"
BIN="$ROOT/bin"
mkdir -p "$BIN"

# ── Output helpers ─────────────────────────────────────────────────────────
c_blue=$'\033[1;34m'; c_green=$'\033[1;32m'; c_yellow=$'\033[1;33m'
c_red=$'\033[1;31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
say()  { printf '%s==>%s %s\n' "$c_blue"  "$c_off" "$*"; }
ok()   { printf '%s ok%s %s\n' "$c_green" "$c_off" "$*"; }
warn() { printf '%s !!%s %s\n' "$c_yellow" "$c_off" "$*"; }
die()  { printf '%s ✖ %s%s\n' "$c_red"  "$*" "$c_off" >&2; exit 1; }

# ── Sanity checks ──────────────────────────────────────────────────────────
[[ "$(uname -s)" == "Linux" ]] || die "This script targets Linux. On Windows use run.ps1."
command -v "$PYTHON_BIN" >/dev/null || die "$PYTHON_BIN not found. Install python3 or pass --python."

PY_VERSION=$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR_MINOR_NUM=$(echo "$PY_VERSION" | awk -F. '{ printf "%d%02d\n", $1, $2 }')
[[ "$PY_MAJOR_MINOR_NUM" -ge 310 ]] || die "Python >=3.10 required (found $PY_VERSION)."
ok "Python $PY_VERSION at $(command -v "$PYTHON_BIN")"

for tool in curl tar; do
    command -v "$tool" >/dev/null || die "$tool not installed (apt install $tool)."
done

# ── 1. ffmpeg (static build with libvmaf + libsvtav1 + NVENC) ──────────────
have_full_ffmpeg() {
    local ff="$1"
    [[ -x "$ff" ]] || return 1
    # Drain the full stream (no `grep -q`): with `set -o pipefail`, an early
    # grep exit gives ffmpeg SIGPIPE (141), which then fails the pipeline.
    "$ff" -hide_banner -filters  2>/dev/null | grep    libvmaf   >/dev/null || return 1
    "$ff" -hide_banner -encoders 2>/dev/null | grep -E '^\s*V[. ]+libsvtav1' >/dev/null || return 1
}

install_static_ffmpeg() {
    # BtbN's GPL build ships libsvtav1 + libvmaf + NVENC; johnvansickle's does not.
    local url="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
    local tmp; tmp=$(mktemp -d)
    say "Downloading static ffmpeg from BtbN/FFmpeg-Builds (~100 MB)…"
    curl -fsSL --retry 3 -o "$tmp/ffmpeg.tar.xz" "$url" || die "ffmpeg download failed"
    say "Extracting…"
    tar -C "$tmp" -xf "$tmp/ffmpeg.tar.xz"
    local ffmpeg_path;  ffmpeg_path=$(find "$tmp" -type f -name ffmpeg  | head -1)
    local ffprobe_path; ffprobe_path=$(find "$tmp" -type f -name ffprobe | head -1)
    [[ -n "$ffmpeg_path" && -n "$ffprobe_path" ]] || die "Could not locate ffmpeg/ffprobe in archive"
    install -m 0755 "$ffmpeg_path"  "$BIN/ffmpeg"
    install -m 0755 "$ffprobe_path" "$BIN/ffprobe"
    rm -rf "$tmp"
}

if (( SKIP_FFMPEG )); then
    say "Skipping ffmpeg setup (--skip-ffmpeg). Verifying system ffmpeg…"
    have_full_ffmpeg "$(command -v ffmpeg || true)" || \
        die "System ffmpeg lacks libvmaf or libsvtav1. Drop --skip-ffmpeg to fetch a static build."
    ok "System ffmpeg has the required features."
else
    if have_full_ffmpeg "$BIN/ffmpeg"; then
        ok "bin/ffmpeg already present and complete"
    else
        install_static_ffmpeg
        have_full_ffmpeg "$BIN/ffmpeg" || die "Installed ffmpeg still missing libvmaf/libsvtav1"
        ok "bin/ffmpeg installed ($("$BIN/ffmpeg" -version | head -1))"
    fi
fi

# ── 2. ab-av1 (sequential CRF-search backend) ──────────────────────────────
install_ab_av1() {
    local api_resp; api_resp=$(curl -fsSL https://api.github.com/repos/alexheretic/ab-av1/releases/latest)
    local url
    url=$(echo "$api_resp" | grep -oE 'https://[^"]+linux-musl[^"]*\.tar\.zst' | head -1)
    [[ -n "$url" ]] || die "Could not locate ab-av1 Linux release URL"
    command -v zstd >/dev/null || die "zstd not installed (apt install zstd) — needed to extract ab-av1"

    local tmp; tmp=$(mktemp -d)
    say "Downloading ab-av1 from $url"
    curl -fsSL --retry 3 -o "$tmp/ab-av1.tar.zst" "$url"
    zstd -d "$tmp/ab-av1.tar.zst" -o "$tmp/ab-av1.tar"
    tar -C "$tmp" -xf "$tmp/ab-av1.tar"
    local bin_path; bin_path=$(find "$tmp" -type f -name ab-av1 | head -1)
    [[ -n "$bin_path" ]] || die "ab-av1 binary not found in archive"
    install -m 0755 "$bin_path" "$BIN/ab-av1"
    rm -rf "$tmp"
}

if [[ -x "$BIN/ab-av1" ]]; then
    ok "bin/ab-av1 already present ($("$BIN/ab-av1" --version 2>&1 | head -1))"
else
    install_ab_av1
    ok "bin/ab-av1 installed ($("$BIN/ab-av1" --version 2>&1 | head -1))"
fi

# ── 3. NVENC sanity check (informational) ──────────────────────────────────
if command -v nvidia-smi >/dev/null; then
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || true)
    if [[ -n "$GPU_NAME" ]]; then
        ok "NVIDIA GPU detected: $GPU_NAME"
        if "$BIN/ffmpeg" -hide_banner -encoders 2>/dev/null | grep hevc_nvenc >/dev/null; then
            ok "  ffmpeg has NVENC encoders compiled in"
        else
            warn "ffmpeg has no NVENC encoders — hardware acceleration disabled"
        fi
    fi
else
    printf '   %s(no NVIDIA GPU detected — software encoders only)%s\n' "$c_dim" "$c_off"
fi

# ── 4. Av1an (optional) ────────────────────────────────────────────────────
install_av1an_pkgs() {
    local pm_install
    if   command -v apt-get >/dev/null; then pm_install="sudo apt-get install -y"
    elif command -v dnf     >/dev/null; then pm_install="sudo dnf install -y"
    elif command -v pacman  >/dev/null; then pm_install="sudo pacman -S --needed --noconfirm"
    elif command -v apk     >/dev/null; then pm_install="sudo apk add"
    else
        warn "Unknown package manager — install vapoursynth + plugins manually."
        return 0
    fi

    say "Installing VapourSynth + plugins via system package manager"
    case "$pm_install" in
        *apt-get*)
            $pm_install vapoursynth python3-vapoursynth vapoursynth-extra-plugins \
                        ffms2 mkvtoolnix av1an 2>/dev/null || \
            $pm_install vapoursynth python3-vapoursynth ffms2 mkvtoolnix || true ;;
        *dnf*)
            $pm_install vapoursynth python3-vapoursynth vapoursynth-plugin-ffms2 \
                        vapoursynth-plugin-lsmashsource mkvtoolnix av1an || true ;;
        *pacman*)
            $pm_install vapoursynth python-vapoursynth vapoursynth-plugin-lsmashsource \
                        vapoursynth-plugin-ffms2 mkvtoolnix-cli av1an || true ;;
        *)  warn "Distro not auto-handled; install VapourSynth + L-SMASH/FFMS2 plugins manually." ;;
    esac
}

install_av1an_binary() {
    if command -v av1an >/dev/null; then
        ok "av1an already on PATH ($(av1an --version 2>&1 | head -1))"
        return 0
    fi
    say "Building av1an from source via cargo (needs Rust toolchain)"
    if ! command -v cargo >/dev/null; then
        warn "cargo not found. Install Rust (https://rustup.rs) and re-run with --with-av1an,"
        warn "or install the av1an package via your distro."
        return 1
    fi
    cargo install av1an --locked
    ok "av1an installed to $HOME/.cargo/bin (ensure that's on PATH)"
}

if (( WITH_AV1AN )); then
    install_av1an_pkgs
    install_av1an_binary || warn "Av1an step skipped — backend will be unavailable in the UI."
fi

# ── 5. Python virtualenv + dependencies ────────────────────────────────────
say "Creating virtual environment in .venv/"
[[ -d "$ROOT/.venv" ]] || "$PYTHON_BIN" -m venv "$ROOT/.venv"
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

say "Installing Python dependencies"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r "$ROOT/requirements.txt"
ok "Dependencies installed."

# ── 6. Smoke test imports ──────────────────────────────────────────────────
say "Verifying app imports"
python -c 'from app.main import app; print("app loads ok")' || die "import smoke test failed"

# ── Done ───────────────────────────────────────────────────────────────────
cat <<EOF

${c_green}Installation complete.${c_off}

Start the server with:

    ${c_blue}./run.sh${c_off}                        # listen on 127.0.0.1:8000
    ${c_blue}./run.sh --host 0.0.0.0 --port 8000${c_off}    # bind all interfaces

Or directly:

    source .venv/bin/activate
    python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

EOF
