# shellcheck shell=bash
# boxen doctor: check that this VM gives what boxen promises (sourced by
# bin/boxen). Each check prints ok, warn or FAIL with what to do; the exit
# status is 1 if anything failed.
#
#   boxen doctor              everything that can be checked from a terminal
#   boxen doctor --lan <host:port>  also: whether a build container can reach
#                                   that LAN service (pick one that's up)

D_FAIL=0 D_WARN=0
d_ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
d_warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; D_WARN=$((D_WARN + 1)); }
d_fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; D_FAIL=$((D_FAIL + 1)); }
d_head() { printf '\n%s\n' "$*"; }

doctor_docker() {
  local so cg tmp owner other
  d_head "Rootless Docker (build containers)"
  if ! command -v docker >/dev/null; then d_fail "docker isn't installed (re-run install.sh)"; return; fi
  if ! so=$(docker info -f '{{json .SecurityOptions}}' 2>/dev/null); then
    d_fail "can't reach rootless Docker at $DOCKER_HOST (systemctl --user status docker)"; return
  fi
  if [[ "$so" == *rootless* ]]; then d_ok "the daemon at $DOCKER_HOST is rootless"
  else d_fail "the daemon at $DOCKER_HOST isn't rootless: root in a build container would be root on the VM"; return; fi
  cg=$(docker info -f '{{.CgroupDriver}}' 2>/dev/null || true)
  if [[ "$cg" == none || -z "$cg" ]]; then
    d_warn "no cgroup driver: memory, CPU and pids limits are ignored (needs cgroup v2 and systemd; see install.sh)"
  else
    d_ok "cgroup driver $cg: limits (pids_limit, mem_limit, cpus) apply"
  fi
  if [[ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" == yes ]]; then
    d_ok "lingering is on: containers and timers run without a login and come back after reboots"
  else
    d_warn "lingering is off: rootless Docker stops when you log out (sudo loginctl enable-linger $(id -un))"
  fi
  if ! docker image inspect "$ENV_IMAGE" >/dev/null 2>&1; then
    d_fail "base image $ENV_IMAGE missing (re-run install.sh)"; return
  fi
  # Root in a container writes a file in a bind-mounted folder: it must be
  # yours on the VM. Another container user maps to a subordinate ID.
  tmp=$(mktemp -d -p "$BOXEN_HOME" .doctor.XXXXXX)
  if docker run --rm --network none -v "$tmp:/t" "$ENV_IMAGE" sh -c 'echo x > /t/root-file' >/dev/null 2>&1; then
    owner=$(stat -c %u "$tmp/root-file" 2>/dev/null || echo "?")
    if [[ "$owner" == "$(id -u)" ]]; then
      d_ok "a file root writes in a build container is yours on the VM (uid $owner)"
    else
      d_fail "a file root wrote in a build container is owned by uid $owner on the VM, not you ($(id -u))"
    fi
  else
    d_fail "couldn't run a test container from $ENV_IMAGE"
  fi
  if docker run --rm --network none --user 1000:1000 -v "$tmp:/t" "$ENV_IMAGE" sh -c 'echo x > /t/user-file' >/dev/null 2>&1; then
    other=$(stat -c %u "$tmp/user-file" 2>/dev/null || echo "?")
    d_ok "(for reference: container uid 1000 writes as uid $other on the VM, so build as root)"
  fi
  # Files the test container made may belong to a subordinate ID: remove
  # them from inside.
  docker run --rm --network none -v "$tmp:/t" "$ENV_IMAGE" sh -c 'rm -rf /t/*' >/dev/null 2>&1 || true
  rmdir "$tmp" 2>/dev/null || true
}

doctor_rootful() {
  d_head "The system Docker daemon (boxen v1)"
  if systemctl is-active --quiet docker.service 2>/dev/null || [[ -S /var/run/docker.sock ]]; then
    if (( ${#V1[@]} )); then
      d_warn "the system Docker daemon is running (v1 projects need it until 'boxen migrate --cleanup')"
    else
      d_warn "the system Docker daemon is running and boxen doesn't use it (sudo systemctl disable --now docker.service docker.socket)"
    fi
  else
    d_ok "the system Docker daemon is off"
  fi
  if id -nG | tr ' ' '\n' | grep -qx docker; then
    d_warn "you're in the docker group: anything unsandboxed you run can be root through it ('boxen migrate --cleanup' removes it, or: sudo gpasswd -d $(id -un) docker)"
  else
    d_ok "you're not in the docker group"
  fi
}

doctor_sandbox() {
  local v
  d_head "Claude Code and its sandbox (agents)"
  if command -v claude >/dev/null; then
    v=$(claude --version 2>/dev/null | head -1 || true)
    d_ok "Claude Code ${v:-installed}"
  else
    d_fail "Claude Code isn't installed on the VM (re-run install.sh)"
  fi
  if command -v bwrap >/dev/null && command -v socat >/dev/null; then
    d_ok "bubblewrap and socat are installed"
  else
    d_fail "the sandbox needs bubblewrap and socat (sudo apt install bubblewrap socat)"
  fi
  if [[ "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" == 1 ]]; then
    if [[ -f /etc/apparmor.d/bwrap ]]; then d_ok "AppArmor lets bwrap create user namespaces (/etc/apparmor.d/bwrap)"
    else d_fail "AppArmor blocks bwrap's user namespaces: re-run install.sh (it adds /etc/apparmor.d/bwrap)"; fi
  fi
  if command -v bwrap >/dev/null; then
    if bwrap --ro-bind / / --dev /dev --proc /proc --unshare-user --unshare-net true 2>/dev/null; then
      d_ok "bwrap can build a sandbox here"
    else
      d_fail "bwrap can't build a sandbox here (bwrap --ro-bind / / --unshare-user --unshare-net true failed)"
    fi
  fi
  # boxen's rules are managed settings (see managed_settings in bin/boxen).
  if [[ ! -f "$MANAGED" ]]; then
    d_fail "boxen's sandbox settings aren't installed ($MANAGED missing): re-run install.sh"
  elif ! jq -e --slurpfile want <(managed_settings) '. == $want[0]' "$MANAGED" >/dev/null 2>&1; then
    d_fail "$MANAGED differs from what this boxen writes: re-run install.sh"
  else
    d_ok "managed settings: sandbox on and required, boxen run/env its only exceptions, runtime and Docker sockets hidden, no bypass mode ($MANAGED)"
  fi
  # Your own settings can add to the lists, and allow rules there decide
  # what the file tools (not sandboxed) may do without asking.
  local f broad extra
  for f in "$CLAUDE_DIR/settings.json" "$CLAUDE_DIR/settings.local.json"; do
    [[ -f "$f" ]] || continue
    extra=$(jq -r '(.sandbox.excludedCommands // []) | map(select(. != "boxen run *" and . != "boxen env *")) | join(", ")' "$f" 2>/dev/null || true)
    [[ -z "$extra" ]] || d_warn "$f takes more commands out of the sandbox: $extra"
    broad=$(jq -r '(.permissions.allow // []) | map(select(test("^(Edit|Write|MultiEdit|NotebookEdit)(\\((\\*|\\*\\*|/\\*\\*|//\\*\\*|~/\\*\\*)\\))?$") or test("^Bash(\\(\\*\\))?$"))) | join(", ")' "$f" 2>/dev/null || true)
    [[ -z "$broad" ]] || d_warn "$f allows without asking: $broad (file edits outside a project folder then don't ask either)"
    if [[ "$(jq -r '.permissions.defaultMode // empty' "$f" 2>/dev/null)" == bypassPermissions ]]; then
      d_warn "$f asks for bypassPermissions mode by default; boxen's managed settings turn that mode off"
    fi
  done
  if grep -qx '@boxen.md' "$CLAUDE_DIR/CLAUDE.md" 2>/dev/null && [[ -f "$CLAUDE_DIR/boxen.md" ]]; then
    d_ok "agents get boxen's instructions (CLAUDE.md imports boxen.md)"
  else
    d_warn "agents won't get boxen's instructions: CLAUDE.md doesn't import boxen.md (re-run install.sh)"
  fi
  cat <<'EOF'
        One check needs a real session. In any project, start Claude and ask it
        to run each of these; every one must fail (don't approve a retry):
          touch ~/sandbox-probe        (Read-only file system)
          docker ps                    (can't connect)
          tmux -L boxen ls             (no server: its socket is hidden)
          systemctl --user status      (can't connect to the bus)
          curl -sI https://example.com (blocked by the sandbox proxy)
        and this one must work:  boxen run echo ok
EOF
}

doctor_github() {
  d_head "GitHub"
  if app_configured; then d_ok "GitHub App: $(app_conf APP_SLUG)"
  else d_warn "GitHub isn't connected (boxen github): agents can't push"; fi
  local t helpers
  if git config --global --get-all "includeIf.gitdir:$SRC_ROOT/.path" 2>/dev/null | grep -xF "$GITCONFIG" >/dev/null; then
    d_ok "git in $SRC_ROOT uses boxen's tokens and identity ($GITCONFIG)"
  else
    d_fail "$HOME/.gitconfig doesn't include $GITCONFIG for $SRC_ROOT (re-run install.sh)"
  fi
  # What git really asks, in a repo under ~/src: boxen's helper must be the
  # last one after the last reset (gh auth setup-git, for one, adds a reset
  # and its own helper after boxen's include).
  t=$(mktemp -d -p "$SRC_ROOT" .boxen-doctor.XXXXXX)
  git init -q "$t" 2>/dev/null
  helpers=$(git -C "$t" config --get-all credential.https://github.com.helper 2>/dev/null | awk 'NF == 0 { n = 0; next } { h[n++] = $0 } END { for (i = 0; i < n; i++) print h[i] }')
  rm -rf "$t"
  if [[ "$helpers" == "!"*" git-credential" && "$helpers" != *$'\n'* ]]; then
    d_ok "in $SRC_ROOT, git asks only boxen for GitHub credentials"
  else
    d_fail "in $SRC_ROOT, git asks other credential helpers too (${helpers//$'\n'/; }): re-run install.sh (moves boxen's include last), and don't run 'gh auth setup-git' on the VM"
  fi
  if systemctl --user is-active --quiet boxen-tokens.timer 2>/dev/null; then
    d_ok "the boxen-tokens timer refreshes tokens every 30 minutes"
  elif crontab -l 2>/dev/null | grep -qF '# boxen-tokens'; then
    d_ok "cron refreshes tokens every 30 minutes"
  else
    d_warn "nothing refreshes tokens (re-run install.sh)"
  fi
  if [[ "$(readlink -f "$(command -v gh 2>/dev/null || true)")" == "$BOXEN_REPO/bin/boxen-gh" ]]; then
    d_ok "gh picks each project's token (boxen-gh)"
  else
    d_warn "gh on your PATH isn't boxen's wrapper, so gh in a project won't use its token (is ~/.local/bin first on PATH?)"
  fi
}

doctor_lan() {
  local target=$1 out rc
  d_head "LAN access from build containers"
  [[ "$target" =~ ^[0-9A-Za-z.:-]+$ ]] || { d_fail "--lan needs host:port (a LAN service that's up, like your router's web page)"; return; }
  [[ "$target" == *:* ]] || target="$target:80"
  rc=0
  out=$(docker run --rm "$ENV_IMAGE" curl -sS -k -m 5 -o /dev/null "http://$target/" 2>&1) || rc=$?
  # Reached: any answer at all, including a refused connection (the host
  # replied). Not reached: a timeout, no route, network unreachable.
  if (( rc == 0 )) || [[ "$out" == *"refused"* ]] || (( rc == 52 || rc == 56 || rc == 35 )); then
    d_warn "a build container reached $target: your VM's firewall allows LAN access from boxen (add an outbound rule if you don't want that)"
  else
    d_ok "a build container can't reach $target (${out:-no answer})"
  fi
}

doctor_main() {
  local lan="" V1=()
  while (( $# )); do
    case $1 in
      --lan) lan=${2:-}; [[ -n "$lan" ]] || die "usage: boxen doctor [--lan <host:port>]"; shift ;;
      *) die "usage: boxen doctor [--lan <host:port>]" ;;
    esac
    shift
  done
  mapfile -t V1 < <(v1_projects)
  doctor_docker
  doctor_rootful
  doctor_sandbox
  doctor_github
  [[ -z "$lan" ]] || doctor_lan "$lan"
  d_head "Projects"
  (( ${#V1[@]} == 0 )) || d_warn "boxen v1 projects to move: ${V1[*]} (boxen migrate)"
  d_ok "$(projects | wc -l) v2 project(s) in $SRC_ROOT"
  echo
  if (( D_FAIL )); then echo "$D_FAIL problem(s), $D_WARN warning(s)"; return 1; fi
  if (( D_WARN )); then echo "no problems, $D_WARN warning(s)"; else echo "no problems"; fi
}
