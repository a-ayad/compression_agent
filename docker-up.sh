#!/usr/bin/env bash
# docker-up.sh — auto-detects host capabilities and starts the
# video-compression-agent container with the right compose stack.
#
# Decision flow:
#   * Builds the main image — Av1an + ab-av1 backends are both baked in.
#   * If an NVIDIA GPU is reachable AND nvidia-container-toolkit is set up,
#     also layers in docker-compose.gpu.yml so ffmpeg's NVENC encoders
#     have actual GPU access at runtime.
#   * If a GPU is present but the container toolkit is missing or the
#     docker daemon doesn't have the nvidia runtime registered, offers
#     to install + configure it (apt or dnf), then restart docker.
#
# Usage:
#   ./docker-up.sh              # auto-detect, prompt before risky steps
#   ./docker-up.sh -y           # auto-detect, non-interactive
#   ./docker-up.sh --cpu        # force CPU build even if a GPU is present
#   ./docker-up.sh --rebuild    # docker compose build --no-cache before up
#   ./docker-up.sh --detect     # report findings, don't build/start
#   ./docker-up.sh --port 8765  # bind a different host port (default 8000)
#
# The standalone CUDA VMAF service is its own app — bring it up via
# ./vmaf-up.sh. The two stacks are independent.
#
# Re-runs are idempotent.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# ── Args ───────────────────────────────────────────────────────────────────
ASSUME_YES=0; FORCE_CPU=0; REBUILD=0; DETECT_ONLY=0
HOST_PORT=8000
while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes)    ASSUME_YES=1; shift ;;
        --cpu)       FORCE_CPU=1; shift ;;
        --rebuild)   REBUILD=1; shift ;;
        --detect)    DETECT_ONLY=1; shift ;;
        --port)      HOST_PORT="$2"; shift 2 ;;
        -h|--help)   sed -n '1,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# ── Output helpers ─────────────────────────────────────────────────────────
c_blue=$'\033[1;34m'; c_green=$'\033[1;32m'; c_yellow=$'\033[1;33m'
c_red=$'\033[1;31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
say()  { printf '%s==>%s %s\n' "$c_blue"  "$c_off" "$*"; }
ok()   { printf '%s ok%s %s\n' "$c_green" "$c_off" "$*"; }
warn() { printf '%s !!%s %s\n' "$c_yellow" "$c_off" "$*"; }
die()  { printf '%s xx %s%s\n' "$c_red"   "$*" "$c_off" >&2; exit 1; }

confirm() {
    (( ASSUME_YES )) && return 0
    local prompt="$1"
    read -r -p "$prompt [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]]
}

# Run cmd as root: prefer current user if already root, else sudo.
sudo_() {
    if [[ $EUID -eq 0 ]]; then "$@"
    else sudo "$@"
    fi
}

# ── Sanity ─────────────────────────────────────────────────────────────────
[[ "$(uname -s)" == "Linux" ]] || die "This script targets Linux."
command -v docker >/dev/null || die "docker not installed."
docker compose version >/dev/null 2>&1 || die "docker compose plugin not installed."

# ── Detect distro ──────────────────────────────────────────────────────────
DISTRO_ID="" ; DISTRO_VER=""
if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRO_ID="${ID:-}"
    DISTRO_VER="${VERSION_ID:-}"
fi

# ── Detect GPU ─────────────────────────────────────────────────────────────
HAS_GPU=0; GPU_NAME=""; DRIVER=""
if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then
    GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)"
    DRIVER="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)"
    [[ -n "$GPU_NAME" ]] && HAS_GPU=1
fi

# Heuristic NVENC support check. NVENC has been on every GeForce/Quadro/
# RTX/Pro/Tesla card since Kepler (2012) *except* a handful of compute-only
# Tesla SKUs. If we hit one of those, downgrade to CPU.
gpu_lacks_nvenc() {
    case "${GPU_NAME,,}" in
        *"tesla k80"*|*"tesla k40"*|*"tesla k20"*|\
        *"tesla m10"*|*"tesla m40"*|*"tesla m60"*|\
        *"a100"*|*"h100"*|*"h200"*|*"b100"*|*"b200"*)
            # Datacenter compute parts: NVENC absent or fused off.
            return 0 ;;
    esac
    return 1
}

# ── Detect nvidia-container-toolkit ─────────────────────────────────────────
HAS_TOOLKIT=0; RUNTIME_OK=0
command -v nvidia-ctk >/dev/null && HAS_TOOLKIT=1
docker info 2>/dev/null | grep -qE 'Runtimes:.*nvidia' && RUNTIME_OK=1

# ── Report ─────────────────────────────────────────────────────────────────
fmt_yes() { printf '%spresent%s'    "$c_green" "$c_off"; }
fmt_no()  { printf '%smissing%s'    "$c_dim"   "$c_off"; }

say "Host detection"
printf '    distro:                 %s %s\n' "${DISTRO_ID:-unknown}" "${DISTRO_VER:-}"
if (( HAS_GPU )); then
    printf '    nvidia GPU:             %s%s%s (driver %s)\n' "$c_green" "$GPU_NAME" "$c_off" "$DRIVER"
    if gpu_lacks_nvenc; then
        printf '    NVENC capability:       %sno (%s is compute-only)%s\n' "$c_yellow" "$GPU_NAME" "$c_off"
    else
        printf '    NVENC capability:       %sassumed yes%s\n' "$c_green" "$c_off"
    fi
else
    printf '    nvidia GPU:             %snone%s\n' "$c_dim" "$c_off"
fi
printf '    nvidia-ctk:             %s\n' "$( ((HAS_TOOLKIT)) && fmt_yes || fmt_no )"
printf '    docker nvidia runtime:  %s\n' "$( ((RUNTIME_OK)) && fmt_yes || fmt_no )"
echo

(( DETECT_ONLY )) && exit 0

# ── Install nvidia-container-toolkit if needed ──────────────────────────────
install_toolkit_apt() {
    say "Installing nvidia-container-toolkit (apt)"
    local key=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    local list=/etc/apt/sources.list.d/nvidia-container-toolkit.list
    sudo_ install -d -m 0755 /etc/apt/keyrings /etc/apt/sources.list.d
    if [[ ! -f $key ]]; then
        curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
            | sudo_ gpg --dearmor -o "$key"
    fi
    if [[ ! -f $list ]]; then
        curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
            | sed "s#deb https://#deb [signed-by=$key] https://#g" \
            | sudo_ tee "$list" >/dev/null
    fi
    sudo_ apt-get update
    sudo_ apt-get install -y nvidia-container-toolkit
}

install_toolkit_dnf() {
    say "Installing nvidia-container-toolkit (dnf)"
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \
        | sudo_ tee /etc/yum.repos.d/nvidia-container-toolkit.repo >/dev/null
    sudo_ dnf install -y nvidia-container-toolkit
}

install_toolkit_pacman() {
    say "Installing nvidia-container-toolkit (pacman)"
    sudo_ pacman -S --needed --noconfirm nvidia-container-toolkit
}

install_nvidia_toolkit() {
    case "$DISTRO_ID" in
        ubuntu|debian|linuxmint|pop)            install_toolkit_apt ;;
        fedora|rhel|centos|rocky|almalinux)     install_toolkit_dnf ;;
        arch|cachyos|endeavouros|manjaro)       install_toolkit_pacman ;;
        *)
            warn "Distro '$DISTRO_ID' not auto-handled."
            warn "Manual instructions:"
            warn "  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
            return 1 ;;
    esac
    say "Configuring docker daemon for nvidia runtime"
    sudo_ nvidia-ctk runtime configure --runtime=docker
    warn "Restarting docker daemon — other running containers will briefly disconnect."
    sudo_ systemctl restart docker
    sleep 2
    docker info 2>/dev/null | grep -qE 'Runtimes:.*nvidia' \
        || die "nvidia runtime still not registered after install."
    ok "nvidia-container-toolkit installed and configured."
}

# ── Decide GPU vs CPU mode ─────────────────────────────────────────────────
USE_GPU=0
if (( FORCE_CPU )); then
    say "Forcing CPU mode (--cpu)"
elif (( HAS_GPU )) && ! gpu_lacks_nvenc; then
    if (( HAS_TOOLKIT && RUNTIME_OK )); then
        USE_GPU=1
    else
        say "GPU is reachable but the docker integration is missing pieces:"
        (( HAS_TOOLKIT )) || echo "      - nvidia-container-toolkit not installed"
        (( RUNTIME_OK ))  || echo "      - docker has no 'nvidia' runtime registered"
        echo
        echo "    To use NVENC inside the container we need to install/configure"
        echo "    nvidia-container-toolkit. This requires sudo and will restart"
        echo "    the docker daemon (briefly disrupting other containers)."
        if confirm "Install and configure nvidia-container-toolkit now?"; then
            install_nvidia_toolkit
            USE_GPU=1
        else
            warn "Continuing without GPU. Software encoders only."
        fi
    fi
elif (( HAS_GPU )); then
    warn "GPU present but doesn't expose NVENC. Software encoders only."
else
    say "No NVIDIA GPU. Software encoders only."
fi

# ── Build & start ──────────────────────────────────────────────────────────
COMPOSE_ARGS=(-f docker-compose.yml)
(( USE_GPU )) && COMPOSE_ARGS+=(-f docker-compose.gpu.yml)

# Wire the requested host port through to the override.
export VCA_HOST_PORT="$HOST_PORT"

if (( REBUILD )); then
    say "Rebuilding image from scratch"
    docker compose "${COMPOSE_ARGS[@]}" build --no-cache
fi

mode="$( ((USE_GPU)) && echo "GPU (NVENC enabled)" || echo "CPU-only" )"
say "Bringing up stack — mode: $mode, host port: $HOST_PORT"
docker compose "${COMPOSE_ARGS[@]}" up -d --build

# ── Post-flight ────────────────────────────────────────────────────────────
sleep 3
if ! docker ps --filter name=video-compression-agent --format '{{.Status}}' \
        | grep -q '^Up'; then
    docker logs --tail 40 video-compression-agent >&2 || true
    die "Container failed to start. See logs above."
fi

if (( USE_GPU )); then
    say "Verifying GPU is visible inside the container"
    if docker exec video-compression-agent nvidia-smi -L >/dev/null 2>&1; then
        ok "GPU passthrough working: $(docker exec video-compression-agent nvidia-smi -L | head -1)"
    else
        warn "nvidia-smi inside container failed — NVENC encodes will fall back to error."
    fi
fi

# Tailscale IP, if available — otherwise just localhost.
TS_IP=""
command -v tailscale >/dev/null && TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"

cat <<EOF

${c_green}Container is up.${c_off}

  Local:     ${c_blue}http://127.0.0.1:${HOST_PORT}${c_off}
EOF
[[ -n "$TS_IP" ]] && \
    printf '  Tailscale: %shttp://%s:%s%s\n' "$c_blue" "$TS_IP" "$HOST_PORT" "$c_off"
cat <<EOF

  Logs:      docker logs -f video-compression-agent
  Stop:      docker compose ${COMPOSE_ARGS[*]} down

  CUDA VMAF service is a separate app — start it with:
      ./vmaf-up.sh

EOF
