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
# Ubuntu/Debian dropped the `vapoursynth` package (not in noble's repos), and
# there is no `av1an` apt package. So on apt systems we build VapourSynth from
# source and install av1an via cargo. dnf/pacman still ship the packages.
#
# Encoder CLIs: av1an drives encoder *binaries* directly (not ffmpeg). x264 and
# x265 come from apt; SVT-AV1's `SvtAv1EncApp` is NOT installed here — the apt
# `svt-av1` is far too old for the libsvtav1* presets, which need v4. Build it
# separately (or use the ab-av1 backend, which reaches SVT-AV1 v4 via ffmpeg).

VS_TAG="${VAPOURSYNTH_TAG:-R72}"   # R72 = VSScript API 4.2; the av1an `vapoursynth`
                                   # crate requests 4.1, which R76+ rejects.

install_av1an_apt() {
    local apt="sudo apt-get install -y --no-install-recommends"
    say "Installing av1an build deps + encoder CLIs via apt"
    # build-essential etc. for VapourSynth; x264/x265 + mkvtoolnix for av1an;
    # libffms2-dev ships a working VapourSynth source plugin.
    $apt build-essential autoconf automake libtool pkg-config nasm git \
         cython3 python3-dev python3-pip libzimg-dev libffms2-dev \
         mkvtoolnix x264 x265 || { warn "apt deps install failed"; return 1; }
    # Ubuntu's Cython (3.0.x) emits code incompatible with Python 3.12's
    # PyLong internals — VapourSynth's Cython module needs >= 3.1.
    pip install --break-system-packages --quiet --upgrade Cython || true
}

build_vapoursynth_source() {
    if command -v vspipe >/dev/null && python3 -c 'import vapoursynth' 2>/dev/null; then
        ok "VapourSynth already present ($(vspipe --version 2>&1 | head -1))"
        return 0
    fi
    local tmp; tmp=$(mktemp -d)
    say "Building VapourSynth $VS_TAG from source (~2 min)"
    git clone --quiet --depth 1 -b "$VS_TAG" https://github.com/vapoursynth/vapoursynth "$tmp/vs" \
        || { warn "VapourSynth clone failed"; return 1; }
    ( cd "$tmp/vs" && ./autogen.sh && ./configure && make -j"$(nproc)" && sudo make install ) \
        || { warn "VapourSynth build failed"; return 1; }
    sudo ldconfig
    # autotools installs the Python module to .../site-packages; Debian/Ubuntu
    # Python only searches .../dist-packages. Copy it across.
    local pv; pv=$(python3 -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    local src="/usr/local/lib/python$pv/site-packages/vapoursynth.so"
    [[ -f "$src" ]] && sudo install -m644 "$src" "/usr/local/lib/python$pv/dist-packages/vapoursynth.so"
    rm -rf "$tmp"
    # FFMS2 source plugin -> VapourSynth's autoload dir (so `ffms2` chunk method works).
    sudo mkdir -p /usr/local/lib/vapoursynth
    local ffms2; ffms2=$(find /usr/lib -name 'libffms2.so*' 2>/dev/null | head -1)
    [[ -n "$ffms2" ]] && sudo ln -sf "$ffms2" /usr/local/lib/vapoursynth/libffms2.so
    command -v vspipe >/dev/null && ok "VapourSynth installed ($(vspipe --version 2>&1 | head -1))"
}

ensure_cargo() {
    # av1an 0.5.x needs rustc >= 1.88; distro cargo is usually older.
    local cargo_bin; cargo_bin=$(command -v cargo || echo "")
    if [[ -n "$cargo_bin" ]]; then
        local v; v=$(cargo --version | awk '{print $2}')
        if [[ "$(printf '%s\n1.88.0\n' "$v" | sort -V | head -1)" == "1.88.0" ]]; then
            return 0
        fi
        warn "cargo $v is too old for av1an; installing rustup toolchain"
    fi
    if ! command -v rustup >/dev/null; then
        curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
            | sh -s -- -y --default-toolchain stable --profile minimal \
            || { warn "rustup install failed"; return 1; }
    fi
    export PATH="$HOME/.cargo/bin:$PATH"
}

install_av1an_binary() {
    if command -v av1an >/dev/null && av1an --version >/dev/null 2>&1; then
        ok "av1an already working ($(av1an --version 2>&1 | head -1))"
        return 0
    fi
    ensure_cargo || return 1
    say "Building av1an from source via cargo"
    # PKG_CONFIG_PATH + -L so the vapoursynth crate finds the source-built libs.
    PKG_CONFIG_PATH="/usr/local/lib/pkgconfig:${PKG_CONFIG_PATH:-}" \
    RUSTFLAGS="-L /usr/local/lib" \
        "$HOME/.cargo/bin/cargo" install av1an --locked --force \
        || { warn "av1an cargo build failed"; return 1; }
    sudo ln -sf "$HOME/.cargo/bin/av1an" /usr/local/bin/av1an
    ok "av1an installed ($(av1an --version 2>&1 | head -1))"
}

install_av1an_other_pm() {
    local apt
    if   command -v dnf    >/dev/null; then apt="sudo dnf install -y"
    elif command -v pacman >/dev/null; then apt="sudo pacman -S --needed --noconfirm"
    else warn "Unknown package manager — install VapourSynth + av1an manually."; return 0; fi
    say "Installing VapourSynth + av1an via system package manager"
    case "$apt" in
        *dnf*)    $apt vapoursynth python3-vapoursynth vapoursynth-plugin-ffms2 \
                       vapoursynth-plugin-lsmashsource mkvtoolnix x264 x265 av1an || true ;;
        *pacman*) $apt vapoursynth python-vapoursynth vapoursynth-plugin-lsmashsource \
                       vapoursynth-plugin-ffms2 mkvtoolnix-cli x264 x265 av1an || true ;;
    esac
}

if (( WITH_AV1AN )); then
    if command -v apt-get >/dev/null; then
        install_av1an_apt && build_vapoursynth_source
        install_av1an_binary || warn "Av1an step incomplete — backend may be unavailable in the UI."
    else
        install_av1an_other_pm
    fi
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
