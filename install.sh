#!/usr/bin/env bash
# boxen installer. On a fresh Ubuntu VM, as your normal user:
#
#   git clone https://github.com/theycallmeboxy/boxen.git ~/boxen
#   ~/boxen/install.sh
#
# Safe to re-run: every step checks whether it's already done.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
BIN_DIR="$HOME/.local/bin"

die()  { printf 'install: %s\n' "$*" >&2; exit 1; }
info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

[[ $EUID -ne 0 ]] || die "run as your normal user, not root (it uses sudo where needed)"
# shellcheck source=/dev/null
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || die "expected Ubuntu, found '${ID:-unknown}'"

# 1. Docker CE + compose plugin, from Docker's own apt repo (not the snap).
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  info "Docker already installed"
else
  info "installing Docker CE from download.docker.com"
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl git
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
if id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
  info "$USER is already in the docker group"
else
  info "adding $USER to the docker group"
  sudo usermod -aG docker "$USER"
fi

# 3. Put the boxen command on your PATH.
mkdir -p "$BIN_DIR"
ln -sfn "$REPO_DIR/bin/boxen" "$BIN_DIR/boxen"
chmod +x "$REPO_DIR/bin/boxen"
info "linked $BIN_DIR/boxen -> $REPO_DIR/bin/boxen"

# 4. Run boxen setup. This shell may predate the docker group change, so use sg if needed.
if docker info >/dev/null 2>&1; then
  "$REPO_DIR/bin/boxen" setup
else
  sg docker -c "$(printf '%q' "$REPO_DIR/bin/boxen") setup"
fi

cat <<EOF

==> Installed. Next:
    1. Log out and back in (picks up the docker group and ~/.local/bin on PATH).
    2. Edit ~/.config/boxen/boxen.env: GitHub token + git identity.
    3. boxen new <name> https://github.com/<you>/<repo>.git
EOF
