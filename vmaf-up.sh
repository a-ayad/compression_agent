#!/usr/bin/env bash
# vmaf-up.sh — bring up the standalone CUDA VMAF service.
#
# The service is independent of the compression agent: its own image,
# its own container, its own port (default 8011), its own upload dir
# (./vmaf-uploads). It needs an NVIDIA GPU + nvidia-container-toolkit
# at runtime; without those, the service starts but every CUDA request
# falls back to CPU libvmaf.
#
# Usage:
#   ./vmaf-up.sh              # auto-detect GPU + toolkit, build, start
#   ./vmaf-up.sh -y           # non-interactive (auto-install toolkit if needed)
#   ./vmaf-up.sh --rebuild    # docker compose build --no-cache
#   ./vmaf-up.sh --port 8765  # bind a different host port
#   ./vmaf-up.sh --down       # stop + remove the container
#
# Re-runs are idempotent.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# ── Args ───────────────────────────────────────────────────────────────────
ASSUME_YES=0; REBUILD=0; DOWN=0
HOST_PORT=8011
while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes)    ASSUME_YES=1; shift ;;
        --rebuild)   REBUILD=1; shift ;;
        --down)      DOWN=1; shift ;;
        --port)      HOST_PORT="$2"; shift 2 ;;
        -h|--help)   sed -n '1,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

c_blue=$'\033[1;34m'; c_green=$'\033[1;32m'; c_yellow=$'\033[1;33m'
c_red=$'\033[1;31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
say()  { printf '%s==>%s %s\n' "$c_blue"  "$c_off" "$*"; }
ok()   { printf '%s ok%s %s\n' "$c_green" "$c_off" "$*"; }
warn() { printf '%s !!%s %s\n' "$c_yellow" "$c_off" "$*"; }
die()  { printf '%s xx %s%s\n' "$c_red"   "$*" "$c_off" >&2; exit 1; }

confirm() {
    (( ASSUME_YES )) && return 0
    read -r -p "$1 [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]]
}

sudo_() {
    if [[ $EUID -eq 0 ]]; then "$@"
    else sudo "$@"
    fi
}

[[ "$(uname -s)" == "Linux" ]] || die "Linux only."
command -v docker >/dev/null || die "docker not installed."
docker compose version >/dev/null 2>&1 || die "docker compose plugin not installed."

# ── --down short-circuits the rest ─────────────────────────────────────────
if (( DOWN )); then
    say "Stopping cuda-vmaf"
    docker compose -f docker-compose.vmaf.yml down
    ok "Stopped."
    exit 0
fi

# ── Detect distro / GPU / toolkit ──────────────────────────────────────────
DISTRO_ID=""
[[ -r /etc/os-release ]] && . /etc/os-release && DISTRO_ID="${ID:-}"

HAS_GPU=0; GPU_NAME=""
if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then
    GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)"
    [[ -n "$GPU_NAME" ]] && HAS_GPU=1
fi

HAS_TOOLKIT=0; RUNTIME_OK=0
command -v nvidia-ctk >/dev/null && HAS_TOOLKIT=1
docker info 2>/dev/null | grep -qE 'Runtimes:.*nvidia' && RUNTIME_OK=1

say "Host detection"
printf '    distro:                 %s\n' "${DISTRO_ID:-unknown}"
if (( HAS_GPU )); then
    printf '    nvidia GPU:             %s%s%s\n' "$c_green" "$GPU_NAME" "$c_off"
else
    printf '    nvidia GPU:             %snone%s\n' "$c_dim" "$c_off"
fi
fmt_y() { printf '%spresent%s' "$c_green" "$c_off"; }
fmt_n() { printf '%smissing%s' "$c_dim"   "$c_off"; }
printf '    nvidia-ctk:             %s\n' "$( ((HAS_TOOLKIT)) && fmt_y || fmt_n )"
printf '    docker nvidia runtime:  %s\n' "$( ((RUNTIME_OK))  && fmt_y || fmt_n )"
echo

# ── Install nvidia-container-toolkit if needed ─────────────────────────────
install_apt()    { sudo_ install -d -m 0755 /etc/apt/keyrings
                   curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
                     | sudo_ gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
                   curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
                     | sed "s#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g" \
                     | sudo_ tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
                   sudo_ apt-get update && sudo_ apt-get install -y nvidia-container-toolkit; }
install_dnf()    { curl -fsSL https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \
                     | sudo_ tee /etc/yum.repos.d/nvidia-container-toolkit.repo >/dev/null
                   sudo_ dnf install -y nvidia-container-toolkit; }
install_pacman() { sudo_ pacman -S --needed --noconfirm nvidia-container-toolkit; }

install_toolkit() {
    say "Installing nvidia-container-toolkit"
    case "$DISTRO_ID" in
        ubuntu|debian|linuxmint|pop)         install_apt ;;
        fedora|rhel|centos|rocky|almalinux)  install_dnf ;;
        arch|cachyos|endeavouros|manjaro)    install_pacman ;;
        *) warn "Distro '$DISTRO_ID' not auto-handled. See:"
           warn "  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
           return 1 ;;
    esac
    say "Configuring docker daemon for nvidia runtime"
    sudo_ nvidia-ctk runtime configure --runtime=docker
    warn "Restarting docker — other running containers will briefly disconnect."
    sudo_ systemctl restart docker
    sleep 2
    docker info 2>/dev/null | grep -qE 'Runtimes:.*nvidia' \
        || die "nvidia runtime still not registered."
    ok "nvidia-container-toolkit ready."
}

if (( HAS_GPU )) && ! (( HAS_TOOLKIT && RUNTIME_OK )); then
    say "GPU is reachable but the docker integration is incomplete."
    (( HAS_TOOLKIT )) || echo "      - nvidia-container-toolkit not installed"
    (( RUNTIME_OK ))  || echo "      - docker has no 'nvidia' runtime registered"
    if confirm "Install + configure nvidia-container-toolkit now?"; then
        install_toolkit
    else
        warn "Continuing without GPU. The service will use CPU libvmaf only."
    fi
fi

# ── Build & start ──────────────────────────────────────────────────────────
mkdir -p ./vmaf-uploads
export VMAF_HOST_PORT="$HOST_PORT"

if (( REBUILD )); then
    say "Rebuilding image from scratch"
    docker compose -f docker-compose.vmaf.yml build --no-cache
fi

say "Bringing up cuda-vmaf on host port $HOST_PORT"
docker compose -f docker-compose.vmaf.yml up -d --build

# ── Post-flight ────────────────────────────────────────────────────────────
sleep 2
docker ps --filter name=cuda-vmaf --format '{{.Status}}' | grep -q '^Up' \
    || { docker logs --tail 40 cuda-vmaf >&2; die "Container failed to start. See logs above."; }

say "Waiting for service health"
healthy=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if docker exec cuda-vmaf python3 -c \
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health',timeout=2)" \
        >/dev/null 2>&1; then healthy=1; break; fi
    sleep 1
done
(( healthy )) && ok "service healthy" || warn "service didn't report healthy in 12s"

# Probe libvmaf_cuda + GPU presence from inside the container.
HEALTH_JSON="$(docker exec cuda-vmaf python3 -c \
    'import urllib.request,sys; sys.stdout.write(urllib.request.urlopen("http://127.0.0.1:8001/health").read().decode())' 2>/dev/null || true)"
if echo "$HEALTH_JSON" | grep -q '"cuda_filter":true' \
   && echo "$HEALTH_JSON" | grep -q '"gpu_present":true'; then
    ok "CUDA path active (libvmaf_cuda + GPU)"
else
    warn "CUDA path NOT active — service will use CPU libvmaf:"
    echo "    $HEALTH_JSON"
fi

TS_IP=""
command -v tailscale >/dev/null && TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"

cat <<EOF

${c_green}cuda-vmaf is up.${c_off}

  UI:           ${c_blue}http://127.0.0.1:${HOST_PORT}${c_off}
EOF
[[ -n "$TS_IP" ]] && \
    printf '  Tailscale:    %shttp://%s:%s%s\n' "$c_blue" "$TS_IP" "$HOST_PORT" "$c_off"
cat <<EOF

  Logs:         docker logs -f cuda-vmaf
  Stop:         ./vmaf-up.sh --down
  Uploads dir:  $ROOT/vmaf-uploads/

EOF
