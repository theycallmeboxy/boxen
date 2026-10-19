# shellcheck shell=bash
# boxen migrate: move a boxen v1 install (Claude inside each project's
# container, on the system Docker daemon) to v2 (Claude on the VM, builds in
# rootless containers). Sourced by bin/boxen.
#
#   boxen migrate             stop v1, bring its Claude config and each
#                             project across; v1's containers and data stay
#                             until --cleanup, so this can be looked at first
#   boxen migrate --cleanup   delete what v1 left on the system daemon, turn
#                             that daemon off and leave the docker group
#
# Per project: projects/<p>/repos and token/ stay (same format); v1's
# compose files, env/ and agent/ move to projects/<p>/v1/; env/Dockerfile and
# env/services.yaml become ~/src/<p>/.boxen/ (FROM and USER lines
# rewritten); sidecar volumes (boxen.<p>.*) are copied to rootless Docker.
# Tool installs in v1's home volumes (boxen-home-<p>) are not copied: what a
# project needs belongs in its Dockerfile.

V1_SOCK=/var/run/docker.sock
v1d() { docker -H "unix://$V1_SOCK" "$@"; }

# v1_image: an image on the system daemon to run tar in.
V1_IMG=""
v1_image() {
  local i
  for i in boxen-base:latest boxen-base:core ubuntu:24.04; do
    if v1d image inspect "$i" >/dev/null 2>&1; then V1_IMG=$i; return 0; fi
  done
  v1d pull -q ubuntu:24.04 >/dev/null && V1_IMG=ubuntu:24.04
}

# deep_merge BASE OVER: OVER's values win; objects merge; lists are joined
# without duplicates (BASE's first).
DEEP_MERGE='
  def m($a; $b):
    if ($a | type) == "object" and ($b | type) == "object" then
      reduce (($a | keys) + ($b | keys) | unique)[] as $k ({};
        .[$k] = (if ($a | has($k)) and ($b | has($k)) then m($a[$k]; $b[$k])
                 elif ($b | has($k)) then $b[$k] else $a[$k] end))
    elif ($a | type) == "array" and ($b | type) == "array" then
      $a + [$b[] | select(. as $x | $a | index([$x]) | not)]
    else $b end;
  m(.[0]; .[1])'

migrate_claude() {
  local tmp f mark="$BOXEN_HOME/.migrated-claude"
  if [[ -f "$mark" ]]; then info "Claude config: moved already ($mark)"; return 0; fi
  if ! v1d volume inspect claude-config >/dev/null 2>&1; then
    info "Claude config: no v1 claude-config volume; nothing to move"
    return 0
  fi
  info "Claude config: copying v1's shared config (login, settings, sessions, memory) into $CLAUDE_DIR"
  tmp=$(mktemp -d)
  v1d run --rm --user 0 --network none --entrypoint tar -v claude-config:/v:ro "$V1_IMG" \
      -C /v --exclude ./sessions --exclude ./shell-snapshots -cf - . \
    | tar -C "$tmp" --no-same-owner -xf - \
    || { rm -rf "$tmp"; die "couldn't copy the claude-config volume"; }
  mkdir -p "$CLAUDE_DIR"
  # settings.json: v1's, with anything set on the VM since on top.
  if [[ -f "$tmp/settings.json" ]]; then
    if [[ -f "$CLAUDE_DIR/settings.json" ]]; then
      jq -s "$DEEP_MERGE" "$tmp/settings.json" "$CLAUDE_DIR/settings.json" > "$tmp/settings.merged" \
        && mv -f "$tmp/settings.merged" "$CLAUDE_DIR/settings.json" \
        || warn "couldn't merge v1's settings.json (kept as $CLAUDE_DIR/settings.json.v1)"
      cp -f "$tmp/settings.json" "$CLAUDE_DIR/settings.json.v1"
    else
      cp "$tmp/settings.json" "$CLAUDE_DIR/settings.json"
    fi
    rm -f "$tmp/settings.json"
  fi
  # .claude.json (v1 kept it inside the config dir; on the VM it's ~/.claude.json):
  # per-folder state (trust, history) comes across; a login made on the VM stays.
  if [[ -f "$tmp/.claude.json" ]]; then
    if [[ -f "$HOME/.claude.json" ]]; then
      jq -s '.[1] as $vm | .[0] as $v1 | $vm | .projects = (($v1.projects // {}) + ($vm.projects // {}))' \
         "$tmp/.claude.json" "$HOME/.claude.json" > "$tmp/claude.merged" \
        && cp -f "$HOME/.claude.json" "$HOME/.claude.json.before-migrate" \
        && mv -f "$tmp/claude.merged" "$HOME/.claude.json" \
        || warn "couldn't merge v1's .claude.json; yours is unchanged"
    else
      (umask 077; cp "$tmp/.claude.json" "$HOME/.claude.json")
    fi
    rm -f "$tmp/.claude.json"
  fi
  # CLAUDE.md: v1's, if the VM's is only boxen's import line.
  if [[ -f "$tmp/CLAUDE.md" ]]; then
    if [[ ! -f "$CLAUDE_DIR/CLAUDE.md" ]] || ! grep -vqx -e '@boxen.md' -e '' "$CLAUDE_DIR/CLAUDE.md"; then
      cp -f "$tmp/CLAUDE.md" "$CLAUDE_DIR/CLAUDE.md"
    else
      cp -f "$tmp/CLAUDE.md" "$CLAUDE_DIR/CLAUDE.md.v1"
      warn "both v1 and the VM have a CLAUDE.md of their own; v1's is $CLAUDE_DIR/CLAUDE.md.v1"
    fi
    rm -f "$tmp/CLAUDE.md"
  fi
  rm -f "$tmp/boxen.md"
  rm -rf "$tmp/skills/carry"
  # Everything else (sessions, memory, skills, todos...) where the VM has
  # nothing at that path.
  cp -an "$tmp/." "$CLAUDE_DIR/"
  rm -rf "$tmp"
  claude_config
  : > "$mark"
  info "Claude config: moved (sessions are at the same paths, so 'claude --resume' in a project finds them)"
}

# v1_dockerfile < v1's env/Dockerfile: the v2 one.
v1_dockerfile() {
  printf '%s\n' "# Moved from boxen v1 by 'boxen migrate': FROM now names boxen-env, and the" \
                "# USER lines are gone (you're root in v2's build container)."
  sed -E -e 's/^([[:space:]]*FROM[[:space:]]+(--[^[:space:]]+[[:space:]]+)*)boxen-base(:[A-Za-z0-9._-]+)?([[:space:]]|$)/\1boxen-env:latest\4/I' \
         -e '/^[[:space:]]*USER[[:space:]]/Id'
}

migrate_project() {
  local n=$1 pdir src v1dir f vols v
  pdir=$(proj_dir "$n")
  src=$(src_dir "$n")
  v1dir="$pdir/v1"
  info "$n"
  [[ -d "$src" ]] || { warn "$n: $src doesn't exist; skipping"; return 1; }
  mkdir -p "$v1dir"
  # v1's environment, read before its files move.
  if [[ ! -e "$(bdir "$n")" ]]; then
    mkdir -p "$(bdir "$n")"
    if [[ -f "$pdir/env/Dockerfile" && ! -L "$pdir/env/Dockerfile" ]]; then
      v1_dockerfile < "$pdir/env/Dockerfile" > "$(bdir "$n")/Dockerfile"
      diff -u --label "v1 env/Dockerfile" --label ".boxen/Dockerfile" "$pdir/env/Dockerfile" "$(bdir "$n")/Dockerfile" | sed 's/^/    /' || true
    fi
    if [[ -f "$pdir/env/services.yaml" && ! -L "$pdir/env/services.yaml" ]]; then
      cp "$pdir/env/services.yaml" "$(bdir "$n")/services.yaml"
    fi
    env_files "$n" || return 1
    echo "    $src/.boxen/: from v1's env/"
  else
    echo "    $src/.boxen/ exists already; left as it is"
  fi
  for f in compose.yaml compose.services.yaml compose.override.yaml compose.override.example.yaml \
           compose.yaml.old compose.yaml.bak project.env .state-cache agent.log env agent CLAUDE.md; do
    [[ -e "$pdir/$f" || -L "$pdir/$f" ]] && mv -f "$pdir/$f" "$v1dir/"
  done
  if [[ -f "$v1dir/compose.override.yaml" ]]; then
    echo "    your v1 compose.override.yaml is in $v1dir/ (it was for the 'claude' service; v2's is 'env')"
  fi
  write_compose "$n" || return 1
  mkdir_private "$(token_dir "$n")"
  seed_repos "$n"
  # Sidecar data (named boxen.<p>.<vol> on both daemons).
  mapfile -t vols < <(v1d volume ls -q --filter "name=^boxen\\.$n\\." 2>/dev/null || true)
  for v in "${vols[@]}"; do
    [[ "$v" =~ ^boxen\.[a-z0-9_-]+\.[a-z0-9_-]+$ ]] || continue
    if docker volume inspect "$v" >/dev/null 2>&1; then
      echo "    volume $v: already in rootless Docker; not copied again"
      continue
    fi
    docker volume create --label "boxen.project=$n" "$v" >/dev/null
    if v1d run --rm --user 0 --network none --entrypoint tar -v "$v:/v:ro" "$V1_IMG" -C /v --numeric-owner -cf - . \
       | docker run --rm -i --network none -v "$v:/v" "$ENV_IMAGE" tar -C /v --numeric-owner -xpf -; then
      echo "    volume $v: copied"
    else
      warn "$n: copying volume $v failed; v1's copy is untouched"
      docker volume rm "$v" >/dev/null 2>&1 || true
    fi
  done
  start_env "$n" --build || { warn "$n: its build container didn't start (fix $src/.boxen/, then boxen up $n)"; return 1; }
}

migrate_cleanup() {
  local ans left=() imgs=() vols=() ctrs=()
  mapfile -t left < <(v1_projects)
  (( ${#left[@]} == 0 )) || die "these v1 projects haven't moved yet: ${left[*]} (boxen migrate)"
  [[ -t 0 ]] || die "--cleanup asks first; run it in a terminal"
  if [[ -S "$V1_SOCK" ]] && v1d info >/dev/null 2>&1; then
    mapfile -t ctrs < <(v1d ps -aq --filter "name=^boxen-" 2>/dev/null || true)
    mapfile -t imgs < <(v1d images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -E '^boxen-(base|proj-[a-z0-9_-]+):' || true)
    mapfile -t vols < <(v1d volume ls -q 2>/dev/null | grep -E '^(claude-config|boxen-home-[a-z0-9_-]+|boxen\.[a-z0-9_-]+\.[a-z0-9_-]+)$' || true)
    cat <<EOF
This deletes what boxen v1 left on the system Docker daemon:
  ${#ctrs[@]} container(s), ${#imgs[@]} image(s), ${#vols[@]} volume(s): ${vols[*]:-none}
  (claude-config was copied to $CLAUDE_DIR; sidecar volumes to rootless Docker;
   tool installs in boxen-home-* are not kept anywhere)
then turns the system daemon off and takes you out of the docker group (sudo).
EOF
    read -rp "Go ahead? [y/N] " ans
    [[ "$ans" == [yY] ]] || { echo "aborted"; return 0; }
    (( ${#ctrs[@]} == 0 )) || v1d rm -f "${ctrs[@]}" >/dev/null
    (( ${#imgs[@]} == 0 )) || v1d image rm -f "${imgs[@]}" >/dev/null 2>&1 || true
    (( ${#vols[@]} == 0 )) || v1d volume rm "${vols[@]}" >/dev/null 2>&1 || warn "some v1 volumes couldn't be removed (in use?)"
    v1d network prune -f >/dev/null 2>&1 || true
    info "v1's containers, images and volumes are gone"
  else
    read -rp "Turn the system Docker daemon off and leave the docker group? [y/N] " ans
    [[ "$ans" == [yY] ]] || { echo "aborted"; return 0; }
  fi
  sudo systemctl disable --now docker.service docker.socket >/dev/null 2>&1 \
    && info "the system Docker daemon is off" || warn "couldn't stop the system Docker daemon"
  if id -nG | tr ' ' '\n' | grep -qx docker; then
    sudo gpasswd -d "$(id -un)" docker >/dev/null && info "you're out of the docker group (takes effect at your next login)"
  fi
  # The supervisor's old files.
  rm -rf "$PROJECTS_DIR/.v1-supervisor"
  info "cleanup done. ~/src/supervisor (if there) is yours to keep or delete."
}

migrate_main() {
  local n list=() failed=()
  case ${1:-} in
    --cleanup) (( $# == 1 )) || die "usage: boxen migrate [--cleanup]"; migrate_cleanup; return ;;
    "") ;;
    *) die "usage: boxen migrate [--cleanup]" ;;
  esac
  require_setup
  mapfile -t list < <(v1_projects)
  [[ -S "$V1_SOCK" ]] && v1d info >/dev/null 2>&1 \
    || die "can't reach the system Docker daemon, where v1 lives (sudo systemctl start docker; are you in the docker group?)"
  v1_image || die "no image on the system daemon to copy data with (and couldn't pull ubuntu:24.04)"
  lock_global
  info "stopping v1's containers"
  for n in "${list[@]}"; do v1d stop "boxen-$n" >/dev/null 2>&1 || true; done
  v1d ps -q --filter "name=^boxen\\." | xargs -r docker -H "unix://$V1_SOCK" stop >/dev/null 2>&1 || true
  if systemctl --user is-enabled --quiet boxen-agent.service 2>/dev/null; then
    systemctl --user disable --now boxen-agent.service >/dev/null 2>&1 || true
    rm -f "$HOME/.config/systemd/user/boxen-agent.service"
    systemctl --user daemon-reload || true
    info "stopped v1's boxen agent"
  fi
  migrate_claude
  for n in "${list[@]}"; do
    if [[ "$n" == supervisor ]]; then
      rm -rf "$PROJECTS_DIR/.v1-supervisor"
      mv "$(proj_dir supervisor)" "$PROJECTS_DIR/.v1-supervisor"
      info "supervisor: v2 has none; its v1 files are in $PROJECTS_DIR/.v1-supervisor until --cleanup"
      continue
    fi
    locked "$n" migrate_project "$n" || failed+=("$n")
  done
  echo
  (( ${#failed[@]} == 0 )) || warn "these need a look: ${failed[*]}"
  cat <<EOF
==> Moved. Next:
    1. boxen doctor                  check the sandbox and rootless Docker
    2. boxen claude <project>, then /resume (or: cd ~/src/<project> && claude --resume)
                                     your v1 sessions are there
    3. boxen migrate --cleanup       once you're happy: delete v1's leftovers
EOF
}
