# shellcheck shell=bash
# boxen import-mac: the VM side of moving your Claude Code work off a Mac.
#
# Sourced by bin/boxen. The Mac never gets a connection from the VM: you run
# mac/boxen-export on the Mac, which pushes over the SSH you already use to
# reach this VM and calls the `boxen import-mac _*` steps below:
#
#   _begin       report where the Mac should push (staging dir, code root)
#   _paths       list the folders Claude Code was used in, from ~/.claude.json
#   _plan        classify them (probe results on stdin) into an editable plan
#   _show/_edit  print or edit the plan
#   _transfers   list what the Mac should push for the chosen scopes
#   _apply       copy staged Claude data into the shared config, create and
#                start projects, report MCP servers, clean up
#
# Scopes: code, memory (auto memory), sessions (transcripts; includes memory),
# config (CLAUDE.md, rules, skills, agents, commands, output styles, settings
# for reference, MCP list). Default: all.

IMPORT_DIR="$BOXEN_HOME/import-mac"
PLAN="$IMPORT_DIR/plan.tsv"
PROBE="$IMPORT_DIR/probe.tsv"
SELECTED="$IMPORT_DIR/selected.txt"
STAGE="$IMPORT_DIR/stage"
CONFIG_ITEMS=(CLAUDE.md rules skills agents commands output-styles agent-memory settings.json)
ALL_SCOPES="code,memory,sessions,config"

# Claude Code files a folder's sessions and memory under
# ~/.claude/projects/<path with every non-alphanumeric character as ->.
encode_path() { printf '%s' "${1//[^A-Za-z0-9]/-}"; }

sanitize_name() {
  local s=${1,,}
  s=${s//[^a-z0-9]/-}
  while [[ $s == *--* ]]; do s=${s//--/-}; done
  s=${s#-}; s=${s%-}
  s=${s:0:40}; s=${s%-}
  [[ -n $s ]] || s=project
  [[ $s != all ]] || s='all-1'
  printf '%s' "$s"
}

human_kb() {
  local kb=$1
  if (( kb >= 1048576 )); then printf '%d.%dG' $((kb / 1048576)) $((kb % 1048576 * 10 / 1048576))
  elif (( kb >= 1024 )); then printf '%dM' $((kb / 1024))
  else printf '%dK' "$kb"; fi
}

# scopes "code,memory" -> sets want_code=1 etc.
parse_scopes() {
  local s=${1:-all} x
  [[ $s == all ]] && s=$ALL_SCOPES
  want_code=0 want_memory=0 want_sessions=0 want_config=0
  IFS=, read -ra _sc <<<"$s"
  for x in "${_sc[@]}"; do
    case $x in
      code) want_code=1 ;; memory) want_memory=1 ;;
      sessions) want_sessions=1 ;; config) want_config=1 ;;
      *) die "unknown scope '$x' (use: $ALL_SCOPES)" ;;
    esac
  done
}

# Active plan rows as "name<TAB>mac path".
plan_rows() {
  [[ -f $PLAN ]] || die "no import plan yet; run the export from your Mac"
  local line name path
  while IFS= read -r line; do
    [[ -z $line || $line == \#* ]] && continue
    IFS=$'\t' read -r name path _ <<<"$line"
    [[ -n $name && -n $path ]] || die "bad plan line (want NAME<TAB>PATH): $line"
    valid_name "$name"
    [[ $name != all ]] || die "'all' can't be a project name (plan line: $line)"
    printf '%s\t%s\n' "$name" "$path"
  done < "$PLAN"
}

imp_begin() {
  require_setup
  mkdir_private "$BOXEN_HOME"
  mkdir_private "$IMPORT_DIR"
  # Fresh staging every run; an earlier plan (with your names) is kept.
  rm -rf "$STAGE" "$IMPORT_DIR/map.tsv" "$IMPORT_DIR/roots.tsv" "$SELECTED"
  mkdir -p "$STAGE/claude" "$SRC_ROOT"
  printf 'STAGE=%s\nSRC=%s\nPLAN=%s\n' "$STAGE" "$SRC_ROOT" "$([[ -f $PLAN ]] && echo 1 || echo 0)"
}

imp_paths() {
  [[ -f "$STAGE/claude.json" ]] || return 0
  jq -r '(.projects // {}) | keys[]' "$STAGE/claude.json"
}

# stdin: "#home<TAB>/Users/x" then "exists<TAB>git<TAB>kb<TAB>path" per folder.
# With --keep, an existing plan is reused (only the probe is refreshed).
imp_plan() {
  local keep=0 home="" line e g kb p
  [[ ${1:-} == --keep ]] && keep=1
  declare -A ex=() gi=() size=()
  local all=()
  while IFS= read -r line; do
    if [[ $line == \#home$'\t'* ]]; then home=${line#*$'\t'}; continue; fi
    IFS=$'\t' read -r e g kb p <<<"$line"
    [[ -n $p ]] || continue
    ex[$p]=$e gi[$p]=$g size[$p]=${kb:-0}
    all+=("$p")
  done
  [[ -n $home ]] || die "probe is missing the Mac home folder"
  printf '#home\t%s\n' "$home" > "$PROBE"
  for p in "${all[@]}"; do printf '%s\t%s\t%s\t%s\n' "${ex[$p]}" "${gi[$p]}" "${size[$p]}" "$p"; done >> "$PROBE"
  if (( keep )) && [[ -f $PLAN ]]; then
    imp_show
    return
  fi

  # Candidates: folders that still exist and aren't obviously not projects.
  local cands=() skipped=()
  for p in "${all[@]}"; do
    if [[ ${ex[$p]} != 1 ]]; then skipped+=("$p (no longer exists)"); continue; fi
    case $p in
      /|"$home") skipped+=("$p (your home folder)"); continue ;;
      "$home"/.claude|"$home"/.claude/*|/tmp|/tmp/*|/private/*|/var/*)
        skipped+=("$p (system or temporary folder)"); continue ;;
    esac
    cands+=("$p")
  done
  # Shortest paths first, so parents are seen before their subfolders.
  if (( ${#cands[@]} )); then
    mapfile -t cands < <(printf '%s\n' "${cands[@]}" | awk '{ print length, $0 }' | sort -n -k1,1 -s | cut -d' ' -f2-)
  fi

  local roots=() r q found desc
  for p in "${cands[@]}"; do
    found=0
    for r in "${roots[@]}"; do [[ $p == "$r"/* ]] && { found=1; break; }; done
    (( found )) && continue        # inside a project: comes along with it
    desc=0
    for q in "${cands[@]}"; do [[ $q == "$p"/* ]] && { desc=1; break; }; done
    if (( desc )) && [[ ${gi[$p]} != 1 ]]; then
      skipped+=("$p (a folder of projects, not a project itself)")
      continue
    fi
    roots+=("$p")
  done

  # Names: the folder name, made valid and unique.
  declare -A used=()
  local name base n rows=()
  for p in "${roots[@]}"; do
    base=$(sanitize_name "$(basename "$p")")
    name=$base n=2
    while [[ -n ${used[$name]:-} ]]; do name="${base:0:37}-$n"; n=$((n + 1)); done
    used[$name]=1
    local note
    note="$(human_kb "${size[$p]}")"
    [[ ${gi[$p]} == 1 ]] && note+=", git" || note+=", not git"
    [[ -e "$SRC_ROOT/$name" ]] && note+=", ~/src/$name already exists: syncs into it"
    rows+=("$(printf '%s\t%s\t# %s' "$name" "$p" "$note")")
  done

  if (( ${#rows[@]} )); then mapfile -t rows < <(printf '%s\n' "${rows[@]}" | sort); fi
  {
    cat <<'EOF'
# boxen import plan: one project per line, NAME<TAB>MAC FOLDER.
# Rename a project by editing NAME; leave one out by deleting its line or
# starting it with #. Code goes to ~/src/NAME. Folders inside a project
# (subfolders or worktrees you ran Claude in) come along with it.
# Sizes are as on the Mac, before skipping node_modules, virtualenvs and caches.
EOF
    if (( ${#rows[@]} )); then printf '%s\n' "${rows[@]}"; fi
    if (( ${#skipped[@]} )); then
      echo "#"
      echo "# Not imported (uncomment and name one to include it anyway):"
      for p in "${skipped[@]}"; do printf '#   %s\n' "$p"; done
    fi
  } > "$PLAN"
  imp_show
}

# Projects are numbered for selection; with a selection in place for this
# run, the unselected ones are marked.
imp_show() {
  [[ -f $PLAN ]] || die "no import plan yet"
  local line i=0 name mark
  echo
  while IFS= read -r line; do
    [[ $line == \#home* ]] && continue
    if [[ $line == \#* ]]; then printf '  \033[2m%s\033[0m\n' "$line"; continue; fi
    i=$((i + 1))
    name=${line%%$'\t'*}
    mark=" "
    if [[ -f $SELECTED ]]; then
      grep -qxF "$name" "$SELECTED" && mark="*" || mark="-"
    fi
    printf '  %s %2d) %s\n' "$mark" "$i" "${line//$'\t'/   }"
  done < "$PLAN"
  if [[ -f $SELECTED ]]; then
    printf '\n  This run: %s project(s) marked *. Those marked - are left for another run.\n' "$(wc -l < "$SELECTED")"
  fi
  echo
}

# _select TOKEN...: choose which plan projects this run covers, by number
# (as shown), range (3-5) or name. "all" clears the selection.
imp_select() {
  local rows=() names=() arg t a b i chosen=()
  mapfile -t rows < <(plan_rows)
  for t in "${rows[@]}"; do names+=("${t%%$'\t'*}"); done
  (( $# )) || die "select projects by number, range (2-4) or name"
  if [[ $1 == all ]]; then rm -f "$SELECTED"; imp_show; return; fi
  for arg in "$@"; do
    arg=${arg//,/ }
    for t in $arg; do
      if [[ $t =~ ^([0-9]+)-([0-9]+)$ ]]; then
        a=${BASH_REMATCH[1]} b=${BASH_REMATCH[2]}
        for (( i = a; i <= b; i++ )); do
          (( i >= 1 && i <= ${#names[@]} )) || die "no project number $i"
          chosen+=("${names[i-1]}")
        done
      elif [[ $t =~ ^[0-9]+$ ]]; then
        (( t >= 1 && t <= ${#names[@]} )) || die "no project number $t"
        chosen+=("${names[t-1]}")
      else
        printf '%s\n' "${names[@]}" | grep -qxF "$t" || die "no project named '$t' in the plan"
        chosen+=("$t")
      fi
    done
  done
  printf '%s\n' "${chosen[@]}" | sort -u > "$SELECTED"
  imp_show
}

# Plan rows this run covers: the selection if there is one, else all.
run_rows() {
  if [[ -f $SELECTED ]]; then
    plan_rows | awk -F'\t' 'NR == FNR { sel[$0] = 1; next } sel[$1]' "$SELECTED" -
  else
    plan_rows
  fi
}

imp_edit() { "${EDITOR:-nano}" "$PLAN"; }

# The Mac's to-do list for the chosen scopes:
#   CODE<TAB>name<TAB>mac folder<TAB>vm folder
#   DATA<TAB>path relative to ~/.claude
# Also writes the mac-key -> vm-key map that _apply uses.
imp_transfers() {
  parse_scopes "${1:-all}"
  local name path vroot p rel mk vk
  local probed=()
  mapfile -t probed < <(awk -F'\t' 'NR > 1 && $1 == 1 { print $4 }' "$PROBE")
  : > "$IMPORT_DIR/map.tsv"
  : > "$IMPORT_DIR/roots.tsv"
  while IFS=$'\t' read -r name path; do
    vroot="$SRC_ROOT/$name"
    printf '%s\t%s\n' "$name" "$path" >> "$IMPORT_DIR/roots.tsv"
    (( want_code )) && printf 'CODE\t%s\t%s\t%s\n' "$name" "$path" "$vroot"
    # The project folder itself, then any folders inside it Claude was used in.
    local inside=("$path")
    for p in "${probed[@]}"; do [[ $p == "$path"/* ]] && inside+=("$p"); done
    for p in "${inside[@]}"; do
      rel=${p#"$path"}
      mk=$(encode_path "$p")
      vk=$(encode_path "$vroot$rel")
      if (( ${#mk} > 200 || ${#vk} > 200 )); then
        warn "skipping Claude data for $p: path too long to map"
        continue
      fi
      printf '%s\t%s\t%s\n' "$mk" "$vk" "$name" >> "$IMPORT_DIR/map.tsv"
      if (( want_sessions )); then printf 'DATA\tprojects/%s\n' "$mk"
      elif (( want_memory )); then printf 'DATA\tprojects/%s/memory\n' "$mk"; fi
    done
  done < <(run_rows | sort -u)
  if (( want_config )); then
    for p in "${CONFIG_ITEMS[@]}"; do printf 'DATA\t%s\n' "$p"; done
  fi
}

# Runs inside a container with the shared config at $CLAUDE_CONFIG_DIR and
# the staged data at /import. Never overwrites anything already on the VM.
# shellcheck disable=SC2016
IMPORT_COPY_SCRIPT='
set -u
cfg=$CLAUDE_CONFIG_DIR imp=${IMPORT_ROOT:-/import} src=${IMPORT_ROOT:-/import}/stage/claude
if [ "$DO_CONFIG" = 1 ]; then
  for f in CLAUDE.md rules skills agents commands output-styles agent-memory; do
    [ -e "$src/$f" ] || continue
    if [ -e "$cfg/$f" ] && [ ! -d "$cfg/$f" ]; then
      echo "  config: kept the VM'"'"'s $f (the Mac'"'"'s copy is in the import staging only)"
      continue
    fi
    cp -a --update=none "$src/$f" "$cfg/" && echo "  config: $f"
  done
  if [ -f "$src/settings.json" ]; then
    cp -a "$src/settings.json" "$cfg/settings.mac.json"
    echo "  config: settings.json saved as settings.mac.json (not active; merge by hand)"
  fi
fi
while IFS="	" read -r mk vk name; do
  s=$src/projects/$mk
  [ -d "$s" ] || continue
  d=$cfg/projects/$vk
  mkdir -p "$d"
  if [ "$DO_SESSIONS" = 1 ]; then
    cp -a --update=none "$s/." "$d/"
  elif [ -d "$s/memory" ]; then
    mkdir -p "$d/memory"
    cp -a --update=none "$s/memory/." "$d/memory/"
  fi
  m=0; [ -d "$d/memory" ] && m=$(find "$d/memory" -type f | wc -l)
  t=$(find "$d" -maxdepth 1 -name "*.jsonl" | wc -l)
  echo "  $name: $m memory files, $t sessions ($vk)"
done < "$imp/map.tsv"
'

imp_apply() {
  parse_scopes "${1:-all}"
  require_setup
  [[ -f "$IMPORT_DIR/map.tsv" ]] || die "nothing staged; run the export from your Mac"

  # 1. Claude data into the shared config.
  if (( want_config || want_memory || want_sessions )); then
    info "copying Claude data into the shared config (nothing on the VM is overwritten)"
    local args; mapfile -t args < <(base_args)
    run_base "${args[@]}" -v "$IMPORT_DIR:/import:ro" \
      -e DO_CONFIG="$want_config" -e DO_SESSIONS="$want_sessions" \
      "$BASE_IMAGE" bash -c "$IMPORT_COPY_SCRIPT"
  fi

  # 2. MCP servers: listed, not installed (they often point at Mac programs).
  if (( want_config )) && [[ -f "$STAGE/claude.json" ]]; then
    local mcp="$BOXEN_HOME/mac-mcp.json" names
    jq '{user: (.mcpServers // {}),
         projects: ((.projects // {}) | with_entries(select((.value.mcpServers // {}) != {}))
                                      | map_values(.mcpServers))}' \
      "$STAGE/claude.json" > "$mcp"
    chmod 600 "$mcp"
    names=$(jq -r '[(.user | keys[]), (.projects | to_entries[] | .value | keys[])] | unique | join(", ")' "$mcp")
    if [[ -n $names ]]; then
      info "MCP servers you'd added on the Mac: $names"
      echo "    Their full settings are in $mcp. Re-add the ones that make sense here"
      echo "    with 'boxen claude <name>' and /mcp (or 'claude mcp add' in 'boxen shell')."
      echo "    claude.ai connectors follow your account and need nothing."
    fi
  fi

  # 3. Projects for the code that came over.
  if (( want_code )); then
    local name path created=()
    while IFS=$'\t' read -r name path; do
      [[ -d "$SRC_ROOT/$name" ]] || { warn "$name: no code arrived in ~/src/$name; skipping"; continue; }
      if [[ -f "$(proj_dir "$name")/compose.yaml" ]]; then
        info "$name: already a boxen project; code synced into it"
        continue
      fi
      create_project "$name" && created+=("$name")
    done < "$IMPORT_DIR/roots.tsv"

    # Start them, the same way `boxen up` does. With a terminal, the one-time
    # prompts (trust the folder; enable Remote Control) are answered here;
    # without one, the line per project says to run 'boxen up <name>' from one.
    if (( ${#created[@]} )); then
      echo
      if [[ -t 0 ]]; then ensure_login; fi
      for name in "${created[@]}"; do
        start_project "$name" || true
      done
    fi
  fi

  # 4. The staged copies include transcripts; don't leave them lying around.
  rm -rf "$STAGE"
  info "import done. The plan is kept in $PLAN for re-runs."
}

imp_help() {
  local ip
  ip=$(hostname -I 2>/dev/null | awk '{ print $1 }')
  cat <<EOF
Move your Claude Code work from a Mac into boxen. It runs from the Mac and
pushes over SSH, so this VM never connects to your LAN.

On your Mac (Terminal), with the address you use to SSH into this VM:

  scp $USER_NAME@${ip:-<this-vm>}:$BOXEN_REPO/mac/boxen-export ~/boxen-export
  bash ~/boxen-export $USER_NAME@${ip:-<this-vm>}

That brings everything: code, auto memory, session transcripts, and your
personal config (CLAUDE.md, rules, skills, agents, commands). It shows a
plan you can edit before anything is copied. For part of it:

  bash ~/boxen-export $USER_NAME@${ip:-<this-vm>} --only memory,config

Scopes: code, memory, sessions, config. Re-running syncs again.
EOF
}

cmd_import_mac() {
  local sub=${1:-}
  [[ $# -gt 0 ]] && shift
  case $sub in
    "")          imp_help ;;
    _begin)      imp_begin ;;
    _paths)      imp_paths ;;
    _plan)       imp_plan "$@" ;;
    _show)       imp_show ;;
    _select)     imp_select "$@" ;;
    _edit)       imp_edit ;;
    _transfers)  imp_transfers "$@" ;;
    _apply)      imp_apply "$@" ;;
    *)           die "unknown import-mac step '$sub' (run 'boxen import-mac' for instructions)" ;;
  esac
}
