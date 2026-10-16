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
warn() { printf 'install: %s\n' "$*" >&2; }
info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

# The boxen agent (bin/boxen-agentd): the listener the supervisor project
# talks to. A systemd user service with lingering, so it runs without a login
# session and comes back after reboots; where `systemctl --user` doesn't
# work, a cron @reboot line (tagged "# boxen-agent") plus a start now.
# Idempotent: the unit is rewritten only when it changed, and a running
# service is restarted when the unit changed or on self-update, so it picks
# up an updated agentd. Run from the agent itself (self-update; BOXEN_AGENT
# set) it doesn't restart the service out from under the running command:
# agentd notices its file changed and re-executes itself once the command
# is done.
install_agent() {
  local boxen_home="${BOXEN_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/boxen}"
  local boxen_src="${BOXEN_SRC:-$HOME/src}"
  local unit_dir="$HOME/.config/systemd/user" unit="boxen-agent.service" tmp was_active=0 changed=0
  local tag="# boxen-agent" job current
  job="BOXEN_HOME=\"$boxen_home\" BOXEN_SRC=\"$boxen_src\" \"$REPO_DIR/bin/boxen-agentd\" >>\"$boxen_home/agent.log\" 2>&1"
  chmod +x "$REPO_DIR/bin/boxen-agentd"
  mkdir -p "$boxen_home"

  if systemctl --user show-environment >/dev/null 2>&1; then
    mkdir -p "$unit_dir"
    tmp=$(mktemp)
    cat > "$tmp" <<EOF
# Written by boxen's install.sh; rewritten on self-update.
[Unit]
Description=boxen agent: runs allowlisted boxen commands for the supervisor project

[Service]
ExecStart="$REPO_DIR/bin/boxen-agentd"
Environment="BOXEN_HOME=$boxen_home"
Environment="BOXEN_SRC=$boxen_src"
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF
    if ! cmp -s "$tmp" "$unit_dir/$unit"; then
      mv -f "$tmp" "$unit_dir/$unit"
      chmod 0644 "$unit_dir/$unit"
      changed=1
    else
      rm -f "$tmp"
    fi
    systemctl --user daemon-reload
    systemctl --user is-active --quiet "$unit" && was_active=1
    # Switching from the cron fallback: stop the agentd it started by hand
    # (never from under the agent itself: that would be this very command).
    if (( ! was_active )) && [[ -z "${BOXEN_AGENT:-}" ]]; then
      pkill -u "$ME" -xf "python3 $REPO_DIR/bin/boxen-agentd" 2>/dev/null || true
    fi
    systemctl --user enable --now "$unit" >/dev/null 2>&1 || die "couldn't start the boxen agent (systemctl --user status $unit)"
    if (( was_active )) && [[ -z "${BOXEN_AGENT:-}" ]] && { (( changed )) || [[ -n "${BOXEN_SELF_UPDATE:-}" ]]; }; then
      systemctl --user restart "$unit"
    fi
    # Without lingering, user services stop when the last session ends.
    if [[ "$(loginctl show-user "$ME" -p Linger --value 2>/dev/null)" != yes ]]; then
      loginctl enable-linger "$ME" 2>/dev/null \
        || warn "couldn't enable lingering (loginctl enable-linger $ME); the agent stops when you log out"
    fi
    # A cron fallback from an earlier install isn't needed any more.
    current=$(crontab -l 2>/dev/null || true)
    if grep -qF "$tag" <<<"$current"; then
      grep -vF "$tag" <<<"$current" | crontab -
    fi
    info "boxen agent: systemd user service $unit (socket in $boxen_home/agent)"
    return 0
  fi

  command -v crontab >/dev/null || die "neither systemctl --user nor cron is available to run the boxen agent"
  current=$(crontab -l 2>/dev/null || true)
  {
    if [[ -n "$current" ]]; then grep -vF "$tag" <<<"$current" || true; fi
    printf '@reboot sleep 30 && %s %s\n' "$job" "$tag"
  } | crontab -
  if [[ -z "${BOXEN_AGENT:-}" ]]; then
    pkill -u "$ME" -xf "python3 $REPO_DIR/bin/boxen-agentd" 2>/dev/null || true
    sleep 0.5
    BOXEN_HOME="$boxen_home" BOXEN_SRC="$boxen_src" nohup "$REPO_DIR/bin/boxen-agentd" >>"$boxen_home/agent.log" 2>&1 &
    disown
  fi
  info "boxen agent: started with nohup and a cron @reboot line (no systemd user session here)"
}

[[ $EUID -ne 0 ]] || die "run as your normal user, not root (it uses sudo where needed)"
# Run by the boxen agent (boxen self-update from the supervisor project):
# there's no terminal, so sudo can't ask for a password. Steps that need it
# are skipped with a note instead of dying half way.
AGENT=${BOXEN_AGENT:-}
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
  if [[ -n "$AGENT" ]]; then
    warn "new packages needed (${missing[*]}); run install.sh from a terminal on the VM to install them"
  else
    info "installing ${missing[*]}"
    sudo apt-get update
    sudo apt-get install -y "${missing[@]}"
  fi
fi

# 1. Docker CE + compose plugin, from Docker's own apt repo.
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  info "Docker already installed"
elif [[ -n "$AGENT" ]]; then
  warn "Docker CE with the compose plugin is needed; run install.sh from a terminal on the VM to install it"
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
elif [[ -n "$AGENT" ]]; then
  warn "$ME isn't in the docker group; run install.sh from a terminal on the VM"
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

# 5. The boxen agent, for the supervisor project (boxen supervisor).
install_agent

# `boxen self-update` runs this too and prints its own summary.
[[ -z "${BOXEN_SELF_UPDATE:-}" ]] || exit 0

cat <<EOF

==> Installed. Next:
    1. Log out and back in if 'boxen' isn't on your PATH yet.
    2. boxen github                      connect GitHub
    3. boxen new <name> [git-url]        add a project
    4. boxen supervisor                  (optional) run boxen from the Claude app
EOF
