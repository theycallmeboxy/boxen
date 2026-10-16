#!/usr/bin/env bash
# boxen installer. On a fresh Ubuntu VM, as your normal user:
#
#   gh repo clone theycallmeboxy/boxen ~/boxen    (or git clone)
#   ~/boxen/install.sh
#
# Safe to re-run: every step checks whether it's already done.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
BIN_DIR="$HOME/.local/bin"
ME="$(id -un)"

die()  { printf 'install: %s\n' "$*" >&2; exit 1; }
info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

[[ $EUID -ne 0 ]] || die "run as your normal user, not root (it uses sudo where needed)"
# shellcheck source=/dev/null
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || die "expected Ubuntu, found '${ID:-unknown}'"

# The snap build of Docker can't see hidden dirs in your home (~/.config/boxen).
if snap list docker >/dev/null 2>&1 \
   || [[ "$(readlink -f "$(command -v docker 2>/dev/null || echo /nonexistent)")" == /snap/* ]]; then
  die "Docker is installed as a snap, which boxen can't use. Remove it with 'sudo snap remove docker', then re-run."
fi

# 0. Tools boxen itself uses on the VM (GitHub token minting and its cron job).
missing=()
for pkg in ca-certificates curl git jq openssl cron; do
  dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if (( ${#missing[@]} )); then
  info "installing ${missing[*]}"
  sudo apt-get update
  sudo apt-get install -y "${missing[@]}"
fi

# 1. Docker CE + compose plugin, from Docker's own apt repo.
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  info "Docker already installed"
else
  info "installing Docker CE from download.docker.com"
  # Ubuntu's own Docker packages conflict with Docker CE.
  for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do
    if dpkg -s "$pkg" >/dev/null 2>&1; then
      info "removing conflicting package $pkg"
      sudo apt-get remove -y "$pkg"
    fi
  done
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
  sudo systemctl enable --now docker
fi

# 2. Let your user run docker without sudo.
if id -nG "$ME" | tr ' ' '\n' | grep -qx docker; then
  info "$ME is already in the docker group"
else
  info "adding $ME to the docker group"
  sudo usermod -aG docker "$ME"
fi

# 3. Put the boxen command on your PATH.
mkdir -p "$BIN_DIR"
chmod +x "$REPO_DIR/bin/boxen"
ln -sfn "$REPO_DIR/bin/boxen" "$BIN_DIR/boxen"
info "linked $BIN_DIR/boxen -> $REPO_DIR/bin/boxen"

# 4. Run boxen setup. This shell may predate the docker group change, so use
#    sg if needed (the path goes through the environment to avoid quoting issues).
export BOXEN_BIN="$REPO_DIR/bin/boxen"
if docker info >/dev/null 2>&1; then
  "$BOXEN_BIN" setup
else
  # shellcheck disable=SC2016  # expanded by sg's shell, on purpose
  sg docker -c '"$BOXEN_BIN" setup'
fi

# `boxen self-update` runs this too and prints its own summary.
[[ -z "${BOXEN_SELF_UPDATE:-}" ]] || exit 0

cat <<EOF

==> Installed. Next:
    1. Log out and back in if 'boxen' isn't on your PATH yet.
    2. boxen github                      connect GitHub
    3. boxen new <name> [git-url]        add a project
EOF
