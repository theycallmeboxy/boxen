# shellcheck shell=bash
# boxen migrate: move a boxen v1 install (Claude inside each project's
# container, on the system Docker daemon) to v2 (Claude on the VM, builds in
# rootless containers). Sourced by bin/boxen.
#
#   boxen migrate [--yes]     stop v1 (asks first, unless --yes), bring its
#                             Claude config, commit identity, base-image
#                             additions and each project across; v1's
#                             containers and data stay until --cleanup
#   boxen migrate --cleanup   delete what v1 left (on the system daemon and in
#                             ~/.config/boxen), turn that daemon off and leave
#                             the docker group
#
# Per project: sidecar volumes (boxen.<p>.*) are copied to rootless Docker
# first (a failure leaves the project as it was); env/ becomes
# ~/src/<p>/.boxen/ (the Dockerfile's FROM and USER lines rewritten);
# projects/<p>/repos and token/ stay (same format); v1's compose files, env/
# and agent/ move to projects/<p>/v1/.
# Tool installs in v1's home volumes (boxen-home-<p>) are not copied: what a
# project needs belongs in its Dockerfile.

V1_SOCK=/var/run/docker.sock
# The system daemon, as you (in the docker group) or, failing that, through
# sudo (after you've left the group).
V1_SUDO=()
v1d() { "${V1_SUDO[@]}" docker -H "unix://$V1_SOCK" "$@"; }
v1_reach() {
  [[ -S "$V1_SOCK" ]] || return 1
  v1d info >/dev/null 2>&1 && return 0
  V1_SUDO=(sudo)
  v1d info >/dev/null 2>&1 && return 0
  V1_SUDO=()
  return 1
}

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
  # settings.json: v1's, with anything set on the VM since on top, except
  # its permissions and sandbox settings: in v1 the container was the
  # boundary, so a permissive mode or broad allow rules were harmless there;
  # on the VM the file tools aren't sandboxed. The whole v1 file stays as
  # settings.json.v1.
  if [[ -f "$tmp/settings.json" ]]; then
    cp -f "$tmp/settings.json" "$CLAUDE_DIR/settings.json.v1"
    if jq -e '(.permissions // {}) != {} or has("sandbox") or has("skipDangerousModePermissionPrompt")' \
         "$tmp/settings.json" >/dev/null 2>&1; then
      info "Claude config: v1's permission and sandbox settings were for a container and aren't carried over (they're in $CLAUDE_DIR/settings.json.v1)"
    fi
    jq 'del(.permissions, .sandbox, .skipDangerousModePermissionPrompt)' "$tmp/settings.json" > "$tmp/settings.v1" \
      || cp /dev/null "$tmp/settings.v1"
    [[ -s "$tmp/settings.v1" ]] || echo '{}' > "$tmp/settings.v1"
    [[ -f "$CLAUDE_DIR/settings.json" ]] || echo '{}' > "$CLAUDE_DIR/settings.json"
    jq -s "$DEEP_MERGE" "$tmp/settings.v1" "$CLAUDE_DIR/settings.json" > "$tmp/settings.merged" \
      && mv -f "$tmp/settings.merged" "$CLAUDE_DIR/settings.json" \
      || warn "couldn't merge v1's settings.json (it's in $CLAUDE_DIR/settings.json.v1)"
    rm -f "$tmp/settings.json" "$tmp/settings.v1"
  fi
  # .claude.json (v1 kept it inside the config dir; on the VM it's ~/.claude.json):
  # per-folder state (trust, history) comes across; a login made on the VM stays.
  # Signed in on the VM already: that account and its state stay, and v1's
  # folders are added. Not signed in: v1's whole file, with anything the VM
  # has on top (so its login and onboarding come across).
  if [[ -f "$tmp/.claude.json" ]]; then
    if [[ -f "$HOME/.claude.json" ]]; then
      jq -s '.[1] as $vm | .[0] as $v1
             | (if ($vm | has("oauthAccount")) then $vm else $v1 * $vm end)
             | .projects = (($v1.projects // {}) + ($vm.projects // {}))' \
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
  cp -a --update=none "$tmp/." "$CLAUDE_DIR/"
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
  local n=$1 pdir src v1dir f e vols v keys
  pdir=$(proj_dir "$n")
  src=$(src_dir "$n")
  v1dir="$pdir/v1"
  info "$n"
  if [[ ! -d "$src" ]]; then
    warn "$n: its code folder $src isn't there; put it back and run boxen migrate again, or drop the project with: rm -rf $pdir"
    return 1
  fi
  # Sidecar data first (named boxen.<p>.<vol> on both daemons): if a copy
  # fails, nothing else has changed and the project is still v1, so another
  # `boxen migrate` tries again, and --cleanup won't touch v1's copy.
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
      docker volume rm "$v" >/dev/null 2>&1 || true
      warn "$n: copying volume $v failed; nothing changed for $n (v1's data is untouched). Run boxen migrate again."
      return 1
    fi
  done
  # v1's environment folder becomes .boxen/: every file in it (the
  # Dockerfile could COPY them: env/ was its build context) but STATUS.md,
  # with the Dockerfile's FROM and USER lines rewritten. Links aren't copied.
  if [[ ! -e "$(bdir "$n")" ]]; then
    mkdir -p "$(bdir "$n")"
    if [[ -d "$pdir/env" && ! -L "$pdir/env" ]]; then
      for e in "$pdir/env"/* "$pdir/env"/.[!.]*; do
        [[ -e "$e" && ! -L "$e" ]] || continue
        f=${e##*/}
        case $f in
          STATUS.md) ;;
          Dockerfile)
            [[ -f "$e" ]] || continue
            v1_dockerfile < "$e" > "$(bdir "$n")/Dockerfile"
            diff -u --label "v1 env/Dockerfile" --label ".boxen/Dockerfile" "$e" "$(bdir "$n")/Dockerfile" | sed 's/^/    /' || true ;;
          *) cp -RP "$e" "$(bdir "$n")/" ;;
        esac
      done
    fi
    env_files "$n" || return 1
    echo "    $src/.boxen/: from v1's env/"
  else
    echo "    $src/.boxen/ exists already; left as it is"
  fi
  if [[ -f "$pdir/project.env" ]]; then
    keys=$(sed -n 's/^[[:space:]]*\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$pdir/project.env" | paste -sd' ' -)
    echo "    project.env (${keys:-no variables}) isn't used by v2: the agent runs on the VM now. Set build-time ones with ENV in .boxen/Dockerfile; it's kept in $v1dir/"
  fi
  mkdir -p "$v1dir"
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
  # Moved from here on: a failed start is fixed in .boxen/, not by migrate.
  start_env "$n" --build || { warn "$n: moved, but its build container didn't start (fix $src/.boxen/, then boxen up $n)"; return 2; }
}

# migrate_shared: what v1 kept for every project. The commit identity
# (boxen.env) goes to boxen's git config; base.local.Dockerfile becomes
# env.local.Dockerfile (then the base image is rebuilt with it).
migrate_shared() {
  local envf="$BOXEN_HOME/boxen.env" name email
  if [[ -f "$envf" && -z "$(git_ident email)" ]]; then
    # (values as compose's env_file read them; surrounding quotes dropped)
    name=$(sed -n 's/^GIT_AUTHOR_NAME=//p' "$envf" | tail -1 | sed -E 's/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/')
    email=$(sed -n 's/^GIT_AUTHOR_EMAIL=//p' "$envf" | tail -1 | sed -E 's/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/')
    if [[ -n "$email" ]]; then
      write_gitconfig "$name" "$email"
      info "commit identity from v1: ${name:-?} <$email>"
    fi
  fi
  if [[ -f "$BOXEN_HOME/base.local.Dockerfile" && ! -e "$LOCAL_ENV" ]]; then
    sed -E -e 's/^([[:space:]]*FROM[[:space:]]+(--[^[:space:]]+[[:space:]]+)*)boxen-base:core([[:space:]]|$)/\1boxen-env:core\3/I' \
           -e '/^[[:space:]]*USER[[:space:]]/Id' "$BOXEN_HOME/base.local.Dockerfile" > "$LOCAL_ENV"
    info "base.local.Dockerfile (v1's additions for every project) is now $LOCAL_ENV; rebuilding the base image"
    build_base || die "the base image didn't build with $LOCAL_ENV; fix it, then run boxen migrate again"
  fi
}

migrate_cleanup() {
  local ans left=() imgs=() vols=() ctrs=() orphans=() v p
  mapfile -t left < <(v1_projects)
  (( ${#left[@]} == 0 )) || die "these v1 projects haven't moved yet: ${left[*]} (boxen migrate)"
  [[ -t 0 ]] || die "--cleanup asks first; run it in a terminal"
  if v1_reach; then
    # v1's containers: each project's (boxen-<p>) and its sidecars (boxen.<p>.<svc>).
    mapfile -t ctrs < <(v1d ps -aq --filter "name=^boxen[-.]" 2>/dev/null || true)
    # Their images (sidecars' too), and boxen's own.
    mapfile -t imgs < <({ (( ${#ctrs[@]} == 0 )) || v1d inspect -f '{{.Image}}' "${ctrs[@]}" 2>/dev/null
                          v1d images -q --filter "reference=boxen-base" --filter "reference=boxen-proj-*" 2>/dev/null
                        } | sort -u || true)
    mapfile -t vols < <(v1d volume ls -q 2>/dev/null | grep -E '^(claude-config|boxen-home-[a-z0-9_-]+|boxen\.[a-z0-9_-]+\.[a-z0-9_-]+)$' || true)
    # A sidecar volume with no copy in rootless Docker would be lost.
    for v in "${vols[@]}"; do
      [[ "$v" == boxen.*.* ]] || continue
      docker volume inspect "$v" >/dev/null 2>&1 || orphans+=("$v")
    done
    if (( ${#orphans[@]} )); then
      die "these v1 volumes were never copied to rootless Docker: ${orphans[*]}. Copy them first, or remove them yourself (sudo docker volume rm ...) if you don't need them"
    fi
    cat <<EOF
This deletes what boxen v1 left on the system Docker daemon:
  ${#ctrs[@]} container(s), ${#imgs[@]} image(s) and the build cache,
  ${#vols[@]} volume(s): ${vols[*]:-none}
  (claude-config was copied to $CLAUDE_DIR and the sidecar volumes to rootless
   Docker; tool installs in boxen-home-* are not kept anywhere)
and v1's files in $BOXEN_HOME (boxen.env, base.local.Dockerfile, agent/, each
project's v1/ folder); then turns the system daemon and containerd off and
takes you out of the docker group (sudo).
EOF
  else
    cat <<EOF
The system Docker daemon isn't reachable, so nothing on it is deleted (its data
stays in /var/lib/docker). This deletes v1's files in $BOXEN_HOME (boxen.env,
base.local.Dockerfile, agent/, each project's v1/ folder), turns the system
daemon and containerd off and takes you out of the docker group (sudo).
EOF
  fi
  read -rp "Go ahead? [y/N] " ans
  [[ "$ans" == [yY] ]] || { echo "aborted"; return 0; }
  if (( ${#ctrs[@]} + ${#imgs[@]} + ${#vols[@]} )); then
    (( ${#ctrs[@]} == 0 )) || v1d rm -f "${ctrs[@]}" >/dev/null
    (( ${#vols[@]} == 0 )) || v1d volume rm "${vols[@]}" >/dev/null 2>&1 || warn "some v1 volumes couldn't be removed"
    (( ${#imgs[@]} == 0 )) || v1d image rm -f "${imgs[@]}" >/dev/null 2>&1 || true
    v1d builder prune -af >/dev/null 2>&1 || true
    v1d network prune -f >/dev/null 2>&1 || true
    info "v1's containers, images, volumes and build cache are gone"
  fi
  rm -rf "$PROJECTS_DIR/.v1-supervisor" "$BOXEN_HOME/agent" "$BOXEN_HOME/agent.log" \
         "$BOXEN_HOME/boxen.env" "$BOXEN_HOME/base.local.Dockerfile"
  for p in "$PROJECTS_DIR"/*/v1; do [[ -d "$p" ]] && rm -rf "$p"; done
  info "v1's files in $BOXEN_HOME are gone"
  sudo systemctl disable --now docker.service docker.socket containerd.service >/dev/null 2>&1 \
    && info "the system Docker daemon and containerd are off (rootless Docker doesn't use them)" \
    || warn "couldn't stop the system Docker daemon"
  if id -nG | tr ' ' '\n' | grep -qx docker; then
    sudo gpasswd -d "$(id -un)" docker >/dev/null && info "you're out of the docker group (takes effect at your next login)"
  fi
  echo "Kept, for you to look at or delete:"
  for p in "$CLAUDE_DIR/settings.json.v1" "$CLAUDE_DIR/CLAUDE.md.v1" "$HOME/.claude.json.before-migrate" "$SRC_ROOT/supervisor"; do
    [[ -e "$p" ]] && echo "    $p"
  done
  info "cleanup done"
}

migrate_main() {
  local n r yes=0 ans list=() failed=() unstarted=() running=()
  case ${1:-} in
    --cleanup) (( $# == 1 )) || die "usage: boxen migrate [--cleanup | --yes]"; migrate_cleanup; return ;;
    --yes|-y) (( $# == 1 )) || die "usage: boxen migrate [--cleanup | --yes]"; yes=1 ;;
    "") ;;
    *) die "usage: boxen migrate [--cleanup | --yes]" ;;
  esac
  require_setup
  mapfile -t list < <(v1_projects)
  v1_reach || die "can't reach the system Docker daemon, where v1 lives (sudo systemctl start docker)"
  v1_image || die "no image on the system daemon to copy data with (and couldn't pull ubuntu:24.04)"
  mapfile -t running < <(v1d ps --filter "name=^boxen[-.]" --format '{{.Names}}' 2>/dev/null || true)
  if (( ${#running[@]} && ! yes )); then
    [[ -t 0 ]] || die "migrate stops v1's containers first; run it in a terminal, or add --yes"
    echo "This stops v1's running containers (any Claude session in them ends):"
    printf '    %s\n' "${running[@]}"
    read -rp "Go ahead? [y/N] " ans
    [[ "$ans" == [yY] ]] || { echo "aborted"; return 0; }
  fi
  lock_global
  if (( ${#running[@]} )); then
    info "stopping v1's containers"
    v1d stop "${running[@]}" >/dev/null 2>&1 || true
  fi
  if systemctl --user is-enabled --quiet boxen-agent.service 2>/dev/null; then
    systemctl --user disable --now boxen-agent.service >/dev/null 2>&1 || true
    rm -f "$HOME/.config/systemd/user/boxen-agent.service"
    systemctl --user daemon-reload || true
    info "stopped v1's boxen agent"
  fi
  migrate_claude
  migrate_shared
  for n in "${list[@]}"; do
    if [[ "$n" == supervisor ]]; then
      rm -rf "$PROJECTS_DIR/.v1-supervisor"
      mv "$(proj_dir supervisor)" "$PROJECTS_DIR/.v1-supervisor"
      info "supervisor: v2 has none; its v1 files are in $PROJECTS_DIR/.v1-supervisor until --cleanup"
      continue
    fi
    r=0
    locked "$n" migrate_project "$n" || r=$?
    if (( r == 2 )); then unstarted+=("$n"); elif (( r )); then failed+=("$n"); fi
  done
  echo
  (( ${#failed[@]} == 0 )) || warn "not moved (still v1; boxen migrate again tries them again): ${failed[*]}"
  (( ${#unstarted[@]} == 0 )) || warn "moved, but their build containers didn't start (fix .boxen/, then boxen up <name>): ${unstarted[*]}"
  cat <<EOF
==> Moved. Next:
    1. boxen doctor                  check the sandbox and rootless Docker
    2. boxen claude <project>, then /resume (or: cd ~/src/<project> && claude --resume)
                                     your v1 sessions are there
    3. boxen migrate --cleanup       once you're happy: delete v1's leftovers
EOF
}
