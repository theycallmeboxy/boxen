#!/usr/bin/env bash
# boxen installer. On an Ubuntu 24.04 VM, as your normal user (it uses sudo
# where it has to):
#
#   git clone https://github.com/theycallmeboxy/boxen ~/boxen
#   ~/boxen/install.sh
#
# What it sets up: Claude Code and its sandbox (bubblewrap, socat, an
# AppArmor profile for bwrap), rootless Docker for your user (the build
# containers), the GitHub CLI, tmux and vim, then `boxen setup`: the base
# image, boxen's Claude settings and instructions, git settings for ~/src and
# the token timer. Safe to re-run: every step checks whether it's done.
set -euo pipefail
# `boxen self-update` runs this with boxen's DOCKER_HOST set. That would hide
# the active docker context below (and make docker warn about it), so the
# installer goes by the contexts alone; boxen sets its own again when it runs.
unset DOCKER_HOST

REPO_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
BIN_DIR="$HOME/.local/bin"
ME="$(id -un)"
BOXEN_HOME="${BOXEN_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/boxen}"

die()  { printf 'install: %s\n' "$*" >&2; exit 1; }
warn() { printf 'install: %s\n' "$*" >&2; }
info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

[[ $EUID -ne 0 ]] || die "run as your normal user, not root (it uses sudo where needed)"
# shellcheck source=/dev/null
. /etc/os-release
[[ "${ID:-}" == ubuntu ]] || die "expected Ubuntu, found '${ID:-unknown}'"
CODENAME=${UBUNTU_CODENAME:-$VERSION_CODENAME}
ARCH=$(dpkg --print-architecture)

if snap list docker >/dev/null 2>&1; then
  die "Docker is installed as a snap, which boxen can't use. Remove it with 'sudo snap remove docker', then re-run."
fi

# v1 projects (agents in containers, on the system Docker daemon) are still
# here until `boxen migrate`: the system daemon stays on for them.
v1_present() {
  local f
  for f in "$BOXEN_HOME"/projects/*/compose.yaml; do
    [[ -f "$f" ]] || continue
    grep -qxF '# boxen-format: 2' "$f" || return 0
  done
  return 1
}

# apt_missing PKG...: the ones that aren't installed, one per line (nothing
# at all when every one is).
apt_missing() {
  local p out=()
  for p in "$@"; do dpkg -s "$p" >/dev/null 2>&1 || out+=("$p"); done
  (( ${#out[@]} == 0 )) || printf '%s\n' "${out[@]}"
}

# 0. Packages from Ubuntu: what boxen uses on the VM (tokens: curl, jq,
#    openssl; boxen ui: tmux, a Python venv, vim), the sandbox's (bubblewrap,
#    socat) and rootless Docker's (uidmap, dbus-user-session).
mapfile -t missing < <(apt_missing ca-certificates curl git jq openssl gnupg tmux python3-venv vim ripgrep \
                                   bubblewrap socat uidmap dbus-user-session)
if (( ${#missing[@]} )); then
  info "installing ${missing[*]}"
  sudo apt-get update
  sudo apt-get install -y "${missing[@]}"
fi

# 1. The sandbox on Ubuntu 24.04: AppArmor stops bwrap from creating the
#    user namespaces it needs unless it has a profile of its own (this one
#    is from Claude Code's sandboxing docs; it covers bwrap itself, not what
#    runs inside).
if [[ "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" == 1 ]] \
   && [[ ! -f /etc/apparmor.d/bwrap ]]; then
  info "adding an AppArmor profile so bwrap (Claude Code's sandbox) can create user namespaces"
  sudo tee /etc/apparmor.d/bwrap >/dev/null <<'EOF'
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
EOF
  sudo systemctl reload apparmor
fi

# 2. Docker CE with the rootless extras, from Docker's own apt repo.
mapfile -t missing < <(apt_missing docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
                                   docker-compose-plugin docker-ce-rootless-extras)
fresh_docker=0
if (( ${#missing[@]} )); then
  dpkg -s docker-ce >/dev/null 2>&1 || fresh_docker=1
  info "installing Docker (${missing[*]}) from download.docker.com"
  for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do
    if dpkg -s "$pkg" >/dev/null 2>&1; then
      info "removing conflicting package $pkg"
      sudo apt-get remove -y "$pkg"
    fi
  done
  # Docker's repo, unless one is set up already (a second entry with another
  # key file would make apt-get update fail).
  if ! grep -rqs 'download.docker.com' /etc/apt/sources.list /etc/apt/sources.list.d/; then
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc
    sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $CODENAME
Components: stable
Architectures: $ARCH
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  fi
  sudo apt-get update
  # Only what's missing, and nothing already installed upgraded: upgrading
  # docker-ce would restart the system daemon, and v1's containers with it.
  sudo apt-get install -y --no-upgrade "${missing[@]}"
fi
# boxen never uses the system daemon (root in its containers would be root on
# the VM). A fresh install turns it off; with v1 projects still here it stays
# on until `boxen migrate --cleanup`.
if (( fresh_docker )) && ! v1_present; then
  sudo systemctl disable --now docker.service docker.socket >/dev/null 2>&1 || true
fi

# 3. Rootless Docker for you: subordinate IDs, the user daemon, lingering (so
#    it runs without a login and after reboots), and cgroup delegation (so
#    memory and CPU limits work, not just pids).
if ! grep -q "^$ME:" /etc/subuid || ! grep -q "^$ME:" /etc/subgid; then
  info "adding subordinate user and group IDs for $ME"
  sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$ME"
fi
if [[ "$(loginctl show-user "$ME" -p Linger --value 2>/dev/null)" != yes ]]; then
  sudo loginctl enable-linger "$ME"
fi
systemctl --user show-environment >/dev/null 2>&1 \
  || die "systemd user services don't work in this session (log in over SSH as $ME, not with su or sudo, and re-run)"
delegate=/etc/systemd/system/user@.service.d/delegate.conf
if [[ ! -f "$delegate" ]]; then
  info "delegating cgroup controllers to user services (memory and CPU limits; applies after a reboot)"
  sudo mkdir -p "$(dirname "$delegate")"
  printf '[Service]\nDelegate=cpu cpuset io memory pids\n' | sudo tee "$delegate" >/dev/null
  sudo systemctl daemon-reload
fi
if ! systemctl --user is-active --quiet docker.service; then
  info "setting up rootless Docker for $ME"
  force=()
  # Its setup tool won't run beside a reachable system daemon without
  # --force (v1's, until `boxen migrate --cleanup`).
  [[ ! -S /var/run/docker.sock ]] || force=(--force)
  dockerd-rootless-setuptool.sh install "${force[@]}"
  systemctl --user enable --now docker.service
fi
# Plain `docker` in your shell goes to the rootless daemon too (boxen sets
# DOCKER_HOST itself either way).
if docker context inspect rootless >/dev/null 2>&1 \
   && [[ "$(docker context show 2>/dev/null)" != rootless ]]; then
  docker context use rootless >/dev/null
fi

# 4. The GitHub CLI and Claude Code, from their signed apt repos. Claude
#    Code's key fingerprint is checked against the one Anthropic publishes.
if ! dpkg -s gh >/dev/null 2>&1; then
  info "installing the GitHub CLI"
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
  sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$ARCH signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y gh
fi
if ! dpkg -s claude-code >/dev/null 2>&1; then
  info "installing Claude Code"
  tmpkey=$(mktemp)
  curl -fsSL https://downloads.claude.ai/keys/claude-code.asc -o "$tmpkey"
  gpg --show-keys --with-colons "$tmpkey" | grep -q '31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE' \
    || { rm -f "$tmpkey"; die "Claude Code's signing key doesn't have the published fingerprint; not installing"; }
  sudo install -m 0644 "$tmpkey" /etc/apt/keyrings/claude-code.asc
  rm -f "$tmpkey"
  echo "deb [signed-by=/etc/apt/keyrings/claude-code.asc] https://downloads.claude.ai/claude-code/apt/stable stable main" \
    | sudo tee /etc/apt/sources.list.d/claude-code.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y claude-code
fi

# 5. boxen on your PATH, with its gh wrapper (each project's own token in
#    ~/src) and boxen-carry (for /carry).
mkdir -p "$BIN_DIR"
chmod +x "$REPO_DIR"/bin/* "$REPO_DIR"/image/boxen-*
ln -sfn "$REPO_DIR/bin/boxen" "$BIN_DIR/boxen"
ln -sfn "$REPO_DIR/bin/boxen-gh" "$BIN_DIR/gh"
ln -sfn "$REPO_DIR/bin/boxen-carry" "$BIN_DIR/boxen-carry"
info "linked boxen, gh (boxen's wrapper) and boxen-carry into $BIN_DIR"

# 6. What boxen v1 ran on the VM: its agent service (the supervisor's
#    listener) and cron lines.
if systemctl --user cat boxen-agent.service >/dev/null 2>&1; then
  systemctl --user disable --now boxen-agent.service >/dev/null 2>&1 || true
  rm -f "$HOME/.config/systemd/user/boxen-agent.service"
  systemctl --user daemon-reload
  info "removed v1's boxen-agent service"
fi
if command -v crontab >/dev/null && crontab -l 2>/dev/null | grep -qF '# boxen-agent'; then
  crontab -l | grep -vF '# boxen-agent' | crontab -
fi

# 7. boxen setup: the base image, Claude settings and instructions, git
#    settings for ~/src, the token timer, the serve service.
"$REPO_DIR/bin/boxen" setup

# 8. The Claude Code settings boxen's isolation depends on (the sandbox, its
#    only exceptions, what it hides, no bypass mode), as managed settings:
#    they outrank every settings file, so nothing can loosen them. See
#    managed_settings in bin/boxen.
managed=/etc/claude-code/managed-settings.d/boxen.json
tmp=$(mktemp)
"$REPO_DIR/bin/boxen" managed-settings > "$tmp"
if ! sudo cmp -s "$tmp" "$managed" 2>/dev/null; then
  sudo install -D -m 0644 "$tmp" "$managed"
  info "Claude Code: boxen's sandbox settings are in $managed (managed: no settings file overrides them)"
fi
rm -f "$tmp"

# 9. boxen ui's Python environment (Textual), rebuilt only when
#    ui/requirements.lock changes. Everything else works without it.
"$REPO_DIR/ui/install-venv.sh" \
  || warn "boxen ui isn't installed (see above); everything else works. Re-run install.sh to retry."

# A dashboard already running (v1's, or before this update) keeps the code
# it started with: start it again with this one.
if tmux -L boxen has-session -t boxen 2>/dev/null; then
  "$REPO_DIR/bin/boxen" ui --restart >/dev/null 2>&1 || true
fi

[[ -z "${BOXEN_SELF_UPDATE:-}" ]] || exit 0

if v1_present; then
  cat <<EOF

==> Installed. You have boxen v1 projects; move them with:
    boxen migrate          (stops v1's containers; brings your Claude login, sessions and projects across)
    boxen doctor           then check everything
EOF
else
  cat <<EOF

==> Installed. Next:
    1. Log out and back in if 'boxen' isn't on your PATH yet.
    2. claude                        sign in to Claude once (/login), then exit
    3. boxen github                  connect GitHub
    4. boxen new <name> [git-url]    add a project; boxen claude <name> to work in it
    5. boxen doctor                  check the sandbox and rootless Docker
EOF
fi
