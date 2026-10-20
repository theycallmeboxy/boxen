# shellcheck shell=bash
# boxen: GitHub App tokens and per-project repo lists. Sourced by bin/boxen.
# ---------------------------------------------------------------- GitHub
#
# A private GitHub App you own mints short-lived tokens (1 hour). Which repos
# a project's tokens cover is a list you keep in boxen's own folder,
# projects/<name>/repos (see `boxen repos`), which the project's agent can't
# write: its git remotes, which it can change, don't decide what it can
# reach. One token per owner (you, an org) in the list, scoped to that
# owner's listed repos, in projects/<name>/token: gh-token.<owner>, gh-token
# (the default: the first listed repo's owner's) and state.json (each listed
# repo's state). The app's private key is denied to sandboxed commands (see
# claude_settings in bin/boxen). git and gh pick a project's token by the
# folder they run in (`boxen git-credential`, bin/boxen-gh), and a systemd
# user timer (or cron) refreshes them every 30 minutes.

GH_DIR="$BOXEN_HOME/github"
GH_KEY="$GH_DIR/app.pem"
GH_CONF="$GH_DIR/app.conf"
TOKENS_LOG="$BOXEN_HOME/tokens.log"
GH_API="https://api.github.com"
GITSCAN="$BOXEN_REPO/bin/boxen-gitscan"

app_configured() { [[ -s "$GH_KEY" && -s "$GH_CONF" ]]; }
app_conf() { sed -n "s/^$1=//p" "$GH_CONF" 2>/dev/null | tail -1; }
set_app_conf() {
  local tmp="$GH_CONF.tmp"
  { grep -v "^$1=" "$GH_CONF" 2>/dev/null || true; printf '%s=%s\n' "$1" "$2"; } > "$tmp"
  mv -f "$tmp" "$GH_CONF"
}
app_install_url() { printf 'https://github.com/apps/%s/installations/new' "$(app_conf APP_SLUG)"; }

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

# RS256 JWT identifying the app, valid for 9 minutes (iat backdated 60s for
# clock drift, as GitHub recommends).
make_jwt() {
  local now h p s
  now=$(date +%s)
  h=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
  p=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' $((now - 60)) $((now + 540)) "$(app_conf CLIENT_ID)" | b64url)
  s=$(printf '%s.%s' "$h" "$p" | openssl dgst -sha256 -sign "$GH_KEY" -binary | b64url)
  printf '%s.%s.%s' "$h" "$p" "$s"
}

# gh_app_api METHOD PATH [JSON]: call the GitHub API as the app. Prints the
# JSON reply; on failure it still prints JSON with a .message, and fails.
gh_app_api() {
  local out rc=0
  local args=(-s --fail-with-body -X "$1"
              -H "Authorization: Bearer $(make_jwt)"
              -H "Accept: application/vnd.github+json"
              -H "X-GitHub-Api-Version: 2022-11-28")
  if [[ -n "${3:-}" ]]; then args+=(-H "Content-Type: application/json" -d "$3"); fi
  out=$(curl "${args[@]}" "$GH_API$2") || rc=$?
  if (( rc )) && [[ -z "$out" ]]; then
    out=$(printf '{"message":"no reply from GitHub (curl exit %d)"}' "$rc")
  fi
  printf '%s\n' "$out"
  return "$rc"
}
gh_msg() { jq -r '.message // empty' <<<"$1" 2>/dev/null || true; }
# gh_unreachable REPLY: a failed gh_app_api got no answer from GitHub at all
# (the network, an outage page) rather than GitHub saying no.
gh_unreachable() {
  [[ "$(gh_msg "$1")" == "no reply from GitHub"* ]] || ! jq -e 'type == "object"' >/dev/null 2>&1 <<<"$1"
}

# owner and repo: GitHub's own shape (start alphanumeric, no trailing . or -), and no ".."
REPO_SEG='[A-Za-z0-9]([A-Za-z0-9_.-]*[A-Za-z0-9_])?'
repo_shape() { [[ $1 =~ ^$REPO_SEG/$REPO_SEG$ && $1 != *..* ]]; }

# github.com remote URL (https or ssh form) -> owner/repo
github_repo() {
  local u=${1%/}
  u=${u%.git}
  case $u in
    https://github.com/*)   u=${u#https://github.com/} ;;
    git@github.com:*)       u=${u#git@github.com:} ;;
    ssh://git@github.com/*) u=${u#ssh://git@github.com/} ;;
    *)
      # https://user@github.com/ (no / in the user part)
      [[ $u =~ ^https://[^/@]+@github\.com/ ]] || return 1
      u=${u#"${BASH_REMATCH[0]}"} ;;
  esac
  repo_shape "$u" || return 1
  printf '%s\n' "$u"
}

# repo_arg ARG: owner/repo from what someone typed: owner/repo, or a
# github.com URL.
repo_arg() {
  case $1 in
    *://*|*@*) github_repo "$1" ;;
    *)         github_repo "https://github.com/$1" ;;
  esac
}

mkdir_private() { mkdir -p "$1" && chmod 700 "$1"; }

token_dir() { printf '%s/token' "$(proj_dir "$1")"; }
repos_file() { printf '%s/repos' "$(proj_dir "$1")"; }

# listed_repos NAME: the project's repo list, one owner/repo per line as
# written there, the first one first. Comments (#), blank lines and lines
# that aren't a repo are skipped; a repo listed twice (in any case) counts
# once. No list: nothing.
listed_repos() {
  local f line seen=" "
  f=$(repos_file "$1")
  [[ -f "$f" && ! -L "$f" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line=${line%%#*}
    line=${line//[[:space:]]/}
    line=${line%.git}
    repo_shape "$line" || continue
    [[ "$seen" != *" ${line,,} "* ]] || continue
    seen+="${line,,} "
    printf '%s\n' "$line"
  done < "$f"
}

# bad_repo_lines NAME: lines of the list that aren't a repo (hand edits).
bad_repo_lines() {
  local f line r
  f=$(repos_file "$1")
  [[ -f "$f" && ! -L "$f" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    r=${line%%#*}
    r=${r//[[:space:]]/}
    [[ -z "$r" ]] || repo_shape "${r%.git}" || printf '%s\n' "$line"
  done < "$f"
}

repos_header() {
  cat <<EOF
# GitHub repos the $1 project's tokens cover, one owner/repo per line; the
# first is the default for gh. Written by boxen: change it with
# 'boxen repos $1 add|rm owner/repo'. The project's git remotes never change it.
EOF
}

# write_repos NAME < content: replace the list atomically (a temp file in
# the project dir, then mv).
write_repos() {
  local f tmp
  f=$(repos_file "$1")
  tmp=$(mktemp -p "$(proj_dir "$1")" .repos.XXXXXX) || return 1
  cat > "$tmp"
  chmod 0644 "$tmp"
  mv -fT "$tmp" "$f" || { rm -f "$tmp"; return 1; }
}

# origin_repo NAME: owner/repo of the project's origin, if it's on GitHub,
# read from the code folder's git config as text by boxen-gitscan (boxen
# never runs git on a folder the project's agent controls: a repo's config
# can make git run programs).
origin_repo() { "$GITSCAN" origin "$SRC_ROOT/$1" 2>/dev/null || true; }

# found_repos NAME: every GitHub repo the code folder's git configs name:
# all remotes, nested repos, submodules (boxen-gitscan; at most 2 s). Written
# by the agent, so untrusted, but only ever owner/repo.
found_repos() { "$GITSCAN" scan "$SRC_ROOT/$1" 2>/dev/null || true; }

# unlisted_repos NAME: found_repos that aren't in the list.
unlisted_repos() {
  local r listed
  listed=" $(listed_repos "$1" | tr '\n' ' ') "
  listed=${listed,,}
  while IFS= read -r r; do
    repo_shape "$r" || continue
    [[ "$listed" == *" ${r,,} "* ]] || printf '%s\n' "$r"
  done < <(found_repos "$1")
}

# seed_repos NAME [REPO]: a project without a list gets one: REPO (what
# `new` clones; none if empty), else the project's origin now if that's on
# GitHub, else an empty list. Happens once (`new`, `migrate`, or else the
# first token refresh); after that, origin is never asked again.
seed_repos() {
  local n=$1 repo=${2-} f
  [[ -d "$(proj_dir "$n")" ]] || return 0
  f=$(repos_file "$n")
  [[ ! -e "$f" && ! -L "$f" ]] || return 0
  (( $# > 1 )) || repo=$(origin_repo "$n")
  { repos_header "$n"; [[ -z "$repo" ]] || printf '%s\n' "$repo"; } | write_repos "$n"
}

# repo_states NAME REPO...: each repo's state from token/state.json, a
# line each: ok, not-installed, no-access, error, or - (not known yet).
repo_states() {
  local n=$1 f r out
  shift
  (( $# )) || return 0
  f="$(token_dir "$n")/state.json"
  if [[ -f "$f" ]] && out=$(jq -r '(if type == "object" then . else {} end) as $s | $ARGS.positional[]
        | ($s[ascii_downcase] // "-")
        | if IN("ok", "not-installed", "no-access", "error") then . else "-" end' "$f" --args "$@" 2>/dev/null); then
    printf '%s\n' "$out"
    return 0
  fi
  for r in "$@"; do echo "-"; done
}

# github_state NAME STATE...: the GITHUB column of `boxen ls`, from the
# listed repos' states (repo_states) and the token files' age, no network:
# "-" with nothing listed; "token ok" when every listed repo is covered,
# "token partial" when some are, "no token" when none is; "token stale" when
# a token is older than 55 minutes (tokens last an hour and the boxen-tokens
# timer renews them every 30 minutes, so the refresh isn't happening).
github_state() {
  local n=$1 s ok=0 dir
  shift
  (( $# )) || { echo "-"; return; }
  app_configured || { echo "run boxen github"; return; }
  dir=$(token_dir "$n")
  for s in "$@"; do [[ "$s" != ok ]] || ok=$((ok + 1)); done
  # A token from a boxen before repo lists: renewed at the next refresh.
  if [[ ! -f "$dir/state.json" && -f "$dir/gh-token" ]]; then ok=$#; fi
  (( ok )) || { echo "no token"; return; }
  if [[ -n "$(find "$dir" -maxdepth 1 -type f \( -name gh-token -o -name 'gh-token.*' \) -mmin +55 2>/dev/null)" ]]; then
    echo "token stale"
  elif (( ok == $# )); then
    echo "token ok"
  else
    echo "token partial"
  fi
}

# installations: "login id" lines (login in lower case) in INSTALLS, every
# account the app is installed on (you, orgs you added it to). Asked once
# per run; fails when GitHub couldn't be asked. Call it directly, never in
# $( ), or the answer isn't kept.
INSTALLS="" INSTALLS_RC=""
installations() {
  local out
  if [[ -z "$INSTALLS_RC" ]]; then
    INSTALLS_RC=1
    if out=$(gh_app_api GET "/app/installations?per_page=100") \
       && INSTALLS=$(jq -r '.[] | "\(.account.login | ascii_downcase) \(.id)"' <<<"$out" 2>/dev/null); then
      INSTALLS_RC=0
    fi
  fi
  return "$INSTALLS_RC"
}

# Per repo (lower case) after a mint: ok, not-installed, no-access or error
# (see mint_owner); per owner (lower case): its token's expiry.
declare -A RSTATE=() OWNER_EXP=()
set_state() {
  local s=$1 r
  shift
  for r in "$@"; do RSTATE[${r,,}]=$s; done
}

# write_token DIR FILE TOKEN: atomically (a reader never sees half a
# token), mode 0600.
write_token() {
  (umask 077; printf '%s\n' "$3" > "$1/.$2.tmp") && mv -f "$1/.$2.tmp" "$1/$2"
}

# mint_request INSTALLATION REPO...: a token covering just those repos.
mint_request() {
  local inst=$1
  shift
  gh_app_api POST "/app/installations/$inst/access_tokens" \
    "$(jq -nc '{repositories: $ARGS.positional}' --args "${@#*/}")"
}

# mint_owner DIR OWNER REPO...: one token for the REPOs (owner/repo, all
# OWNER's) in DIR/gh-token.<owner>, with one request. Each repo's state goes
# in RSTATE: ok; not-installed (the app isn't on that account: someone
# else's repo, so no token is possible; not an error); no-access (it is,
# but the installation doesn't include this repo); error (GitHub couldn't
# be asked; a token already there is kept). When GitHub refuses the request
# (it does, whole, if any repo is out of reach), each repo is asked about
# alone, and the token is for the rest. Fails on errors only.
mint_owner() {
  local dir=$1 owner=$2 ol=${2,,} inst="" out="" tok r rc=0 ok=()
  shift 2
  if installations; then
    inst=$(awk -v o="$ol" '$1 == o { print $2; exit }' <<<"$INSTALLS")
    if [[ -z "$inst" ]]; then
      set_state not-installed "$@"
      rm -f "$dir/gh-token.$ol"
      return 0
    fi
  else
    # No list of installations: ask about the first repo.
    if out=$(gh_app_api GET "/repos/$1/installation"); then inst=$(jq -r '.id // empty' <<<"$out"); fi
  fi
  if ! [[ "$inst" =~ ^[0-9]+$ ]]; then
    warn "$owner: couldn't find the GitHub App's installation ($(gh_msg "$out")); keeping the token there was"
    set_state error "$@"
    return 1
  fi
  if (( $# > 500 )); then
    warn "$owner: more than 500 repos listed; only the first 500 get a token"
    set_state error "${@:501}"
    set -- "${@:1:500}"
    rc=1
  fi
  if out=$(mint_request "$inst" "$@"); then
    ok=("$@")
  elif gh_unreachable "$out"; then
    warn "$owner: couldn't get a token ($(gh_msg "$out")); keeping the one there was"
    set_state error "$@"
    return 1
  else
    for r in "$@"; do
      if out=$(gh_app_api GET "/repos/$r/installation") && [[ "$(jq -r '.id // empty' <<<"$out")" == "$inst" ]]; then
        ok+=("$r")
      elif gh_unreachable "$out"; then
        set_state error "$r"
        rc=1
      else
        set_state no-access "$r"
        warn "$r: the boxen GitHub App can't access it ($(gh_msg "$out")). Add it to the app's installation: $(app_install_url)"
      fi
    done
    if (( ${#ok[@]} )) && ! out=$(mint_request "$inst" "${ok[@]}"); then
      warn "$owner: couldn't get a token ($(gh_msg "$out"))"
      set_state error "${ok[@]}"
      return 1
    fi
  fi
  if (( ${#ok[@]} == 0 )); then
    (( rc )) || rm -f "$dir/gh-token.$ol"
    return "$rc"
  fi
  tok=$(jq -r '.token // empty' <<<"$out")
  if [[ -z "$tok" ]]; then
    warn "$owner: GitHub's reply had no token"
    set_state error "${ok[@]}"
    return 1
  fi
  write_token "$dir" "gh-token.$ol" "$tok"
  OWNER_EXP[$ol]=$(jq -r '.expires_at // empty' <<<"$out")
  set_state ok "${ok[@]}"
  return "$rc"
}

# mint_project NAME: a token per owner in the project's list (mint_owner),
# the default gh-token (the first listed repo's owner's, or if that has
# none, the next one's that has), no token file for an owner no longer
# listed, and state.json: each listed repo's state. Fails when minting
# failed for some owner.
mint_project() {
  local n=$1 dir r ol def="" f rc=0 args=()
  local -a repos=() owners=()
  local -A by=()
  dir=$(token_dir "$n")
  mkdir_private "$dir"
  RSTATE=() OWNER_EXP=()
  mapfile -t repos < <(listed_repos "$n")
  for r in "${repos[@]}"; do
    ol=${r%%/*}
    ol=${ol,,}
    if [[ -z "${by[$ol]+x}" ]]; then owners+=("$ol"); by[$ol]=""; fi
    by[$ol]+="$r "
  done
  for ol in "${owners[@]}"; do
    # shellcheck disable=SC2086  # owner/repo never has a space
    mint_owner "$dir" "$ol" ${by[$ol]} || rc=1
  done
  for r in "${repos[@]}"; do
    ol=${r%%/*}
    ol=${ol,,}
    if [[ -s "$dir/gh-token.$ol" ]]; then def=$ol; break; fi
  done
  if [[ -n "$def" ]]; then
    write_token "$dir" gh-token "$(cat "$dir/gh-token.$def")"
  else
    rm -f "$dir/gh-token"
  fi
  for f in "$dir"/gh-token.*; do
    [[ -e "$f" ]] || continue
    ol=${f##*/gh-token.}
    [[ -n "${by[$ol]+x}" ]] || rm -f "$f"
  done
  for r in "${repos[@]}"; do args+=("${r,,}" "${RSTATE[${r,,}]:-error}"); done
  (umask 077
   jq -n '$ARGS.positional as $a | [range(0; $a | length; 2) | {key: $a[.], value: $a[. + 1]}] | from_entries' \
     --args "${args[@]}" > "$dir/.state.json.tmp") && mv -f "$dir/.state.json.tmp" "$dir/state.json"
  return "$rc"
}

# repo_state_text STATE: what a repo's state means, in a few words.
repo_state_text() {
  case $1 in
    ok)            echo "ok" ;;
    not-installed) echo "not your repo" ;;
    no-access)     echo "no access" ;;
    error)         echo "no token right now" ;;
    *)             if app_configured; then echo "not checked yet"; else echo "run boxen github"; fi ;;
  esac
}

# Refresh one project's tokens (mint_project), first giving it a repo list
# if it has none (seed_repos). A no-op when GitHub isn't set up. Quiet
# except for failures, unless `boxen tokens` asked for the per-project
# report (TOKENS_VERBOSE).
refresh_project() {
  local n=$1 r ol rc=0 msg repos=()
  seed_repos "$n"
  app_configured || return 0
  mint_project "$n" || rc=1
  [[ -n "${TOKENS_VERBOSE:-}" ]] || return "$rc"
  mapfile -t repos < <(listed_repos "$n")
  if (( ${#repos[@]} == 0 )); then
    printf '  %-24s no GitHub repos listed; no token (boxen repos %s add owner/repo)\n' "$n" "$n"
  fi
  for r in "${repos[@]}"; do
    ol=${r%%/*}
    case ${RSTATE[${r,,}]:-error} in
      ok)            msg="token valid until ${OWNER_EXP[${ol,,}]:-?}" ;;
      not-installed) msg="not your repo: pulls work (public), no token to push" ;;
      no-access)     msg="no access: add it to the app's installation" ;;
      *)             msg="no token right now (see above)" ;;
    esac
    printf '  %-24s %-36s %s\n' "$n" "$r" "$msg"
  done
  return "$rc"
}

# Hidden command: the boxen-tokens timer (or cron) runs `tokens --quiet`;
# `boxen github` runs it to mint tokens for existing projects.
# shellcheck disable=SC2120  # main passes them
cmd_tokens() {
  local n rc=0 list=()
  if [[ "${1:-}" == --quiet ]]; then
    # From the timer: output goes to tokens.log, so stay silent unless something fails.
    QUIET=1
    shift
    if [[ -f "$TOKENS_LOG" ]] && (( $(stat -c %s "$TOKENS_LOG") > 1000000 )); then
      : > "$TOKENS_LOG"
    fi
  else
    TOKENS_VERBOSE=1
  fi
  if ! app_configured; then
    [[ -n "${QUIET:-}" ]] || warn "GitHub isn't set up yet; run 'boxen github'"
    return 0
  fi
  if [[ "${1:-}" == all ]]; then shift; fi
  if (( $# )); then
    for n in "$@"; do require_project "$n"; list+=("$n"); done
  else
    # v1 projects too, until they're migrated: their containers read the
    # same token files.
    mapfile -t list < <(projects; v1_projects | grep -vx supervisor || true)
  fi
  for n in "${list[@]}"; do
    refresh_project "$n" || rc=1
  done
  return "$rc"
}

# install_cron: the fallback for ensure_token_timer where `systemctl --user`
# doesn't work.
install_cron() {
  # The timer's there (this ran from a session without systemd, say): it
  # does the job already.
  [[ ! -f "$HOME/.config/systemd/user/boxen-tokens.timer" ]] || return 0
  if ! command -v crontab >/dev/null; then
    warn "neither systemd user services nor cron work here, so tokens won't refresh on their own"
    return 1
  fi
  local tag="# boxen-tokens" job current
  job="BOXEN_HOME=\"$BOXEN_HOME\" BOXEN_SRC=\"$SRC_ROOT\" \"$BOXEN_REPO/bin/boxen\" tokens --quiet >>\"$TOKENS_LOG\" 2>&1"
  # Read your existing crontab in full first, then rewrite it with our lines
  # replaced (everything else kept as is).
  current=$(crontab -l 2>/dev/null || true)
  {
    if [[ -n "$current" ]]; then grep -vF "$tag" <<<"$current" || true; fi
    printf '*/30 * * * * %s %s\n' "$job" "$tag"
    printf '@reboot sleep 30 && %s %s\n' "$job" "$tag"
  } | crontab -
  info "cron refreshes tokens every 30 minutes and at boot (problems go to $TOKENS_LOG)"
}

# uninstall_cron: drop the cron lines (an earlier boxen's, or the fallback's).
uninstall_cron() {
  local current
  command -v crontab >/dev/null || return 0
  current=$(crontab -l 2>/dev/null || true)
  grep -qF "# boxen-tokens" <<<"$current" || return 0
  { grep -vF "# boxen-tokens" <<<"$current" || true; } | crontab -
}

# read_key DEST [FILE]: copy a PEM private key from FILE, or read one pasted
# into the terminal. A pasted key isn't echoed (it would sit in your scrollback);
# a * appears for each line received instead.
read_key() {
  local dest=$1 src=${2:-} line n=0 started=0 tty=0
  (umask 077; : > "$dest")
  if [[ -n "$src" ]]; then
    src=${src/#\~/$HOME}
    [[ -f $src ]] || { rm -f "$dest"; die "no such file: $src"; }
    cat "$src" > "$dest"
  else
    if [[ -t 0 ]]; then
      tty=1
      stty -echo
      trap 'stty echo 2>/dev/null' EXIT
    fi
    while IFS= read -r line; do
      # Ignore anything before the BEGIN line (stray whitespace, etc).
      if (( ! started )); then
        [[ $line == -----BEGIN* ]] || continue
        started=1
      fi
      printf '%s\n' "$line" >> "$dest"
      n=$((n + 1))
      printf '*' >&2
      [[ $line == -----END* ]] && break
    done
    if (( tty )); then stty echo; trap - EXIT; fi
    printf '\n' >&2
    (( n )) || { rm -f "$dest"; die "no key received"; }
    info "received $n lines"
  fi
  openssl pkey -in "$dest" -noout 2>/dev/null || { rm -f "$dest"; die "that isn't a valid private key"; }
}

cmd_github() {
  [[ -t 0 ]] || die "github needs a terminal"
  local c
  for c in openssl curl jq git; do
    command -v "$c" >/dev/null || die "$c is required (sudo apt install $c)"
  done
  [[ -d "$PROJECTS_DIR" ]] || die "run $BOXEN_REPO/install.sh first"

  local keyfile=""
  while (( $# )); do
    case $1 in
      --reset) rm -f "$GH_KEY" "$GH_CONF"; info "forgot the GitHub App; setting it up again" ;;
      --key)   keyfile=${2:-}; [[ -n "$keyfile" ]] || die "--key needs a path to the .pem file"; shift ;;
      *)       die "usage: boxen github [--reset] [--key file.pem]" ;;
    esac
    shift
  done
  mkdir_private "$GH_DIR"

  # Who you are on GitHub: from the VM's gh login if there is one.
  local uid="" login="" name="" user
  if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
    login=$(gh api user --jq .login)
  else
    read -rp "Your GitHub username: " login
  fi
  [[ -n "$login" ]] || die "need your GitHub username"
  user=$(curl -fsS "$GH_API/users/$login") || die "couldn't look up GitHub user '$login'"
  uid=$(jq -r .id <<<"$user")
  name=$(jq -r '.name // empty' <<<"$user")

  if ! app_configured; then
    local cid url
    url="https://github.com/settings/apps/new?name=boxen-$login"
    url+="&description=Short-lived%20per-repo%20tokens%20for%20boxen%20projects"
    url+="&url=https://github.com/$login&public=false&webhook_active=false"
    url+="&contents=write&pull_requests=write&issues=write&workflows=write"
    url+="&actions=read&checks=read&statuses=read&metadata=read"
    cat <<EOF

Step 1 of 2: create your private GitHub App. Open this link, signed in to
GitHub as $login. It pre-fills the name and permissions:

  $url

Click "Create GitHub App" at the bottom. On the page that follows:
  - copy the Client ID (near the top)
  - under "Private keys", click "Generate a private key" (a .pem file downloads)

EOF
    read -rp "Client ID: " cid
    cid=${cid//[[:space:]]/}
    [[ -n "$cid" ]] || die "need the Client ID"
    if [[ -n "$keyfile" ]]; then
      read_key "$GH_KEY.tmp" "$keyfile"
    else
      echo "Now paste the private key: open the downloaded .pem file, copy all of it"
      echo "(BEGIN through END line), and paste here. It won't be shown; you'll see"
      echo "a * per line. (Or re-run with --key /path/to/file.pem if it's on this VM.)"
      read_key "$GH_KEY.tmp"
    fi
    mv -f "$GH_KEY.tmp" "$GH_KEY"
    set_app_conf CLIENT_ID "$cid"
  fi

  local app slug
  app=$(gh_app_api GET /app) || \
    die "GitHub rejected the app's credentials ($(gh_msg "$app")). Check the Client ID and key, then run 'boxen github --reset'."
  slug=$(jq -r .slug <<<"$app")
  set_app_conf APP_SLUG "$slug"
  info "GitHub App: $slug"

  local installs
  installs=$(gh_app_api GET /app/installations | jq length) || die "couldn't list the app's installations"
  if [[ "$installs" == 0 ]]; then
    cat <<EOF

Step 2 of 2: install the app on your account:

  $(app_install_url)

Choose "All repositories" so repos you create later are covered too, or pick
specific ones (then add new repos to it as you go).

EOF
    read -rp "Press Enter once it's installed... " _
    installs=$(gh_app_api GET /app/installations | jq length) || die "couldn't list the app's installations"
    [[ "$installs" != 0 ]] || die "the app isn't installed yet; install it, then re-run 'boxen github'"
  fi
  info "app is installed"

  # Commit identity for boxen projects (repos under ~/src): your GitHub
  # no-reply address keeps your real email out of commits. Kept in boxen's
  # git config (write_gitconfig); one you've set there already stays.
  local email="${uid}+${login}@users.noreply.github.com"
  name=${name:-$login}
  if [[ -z "$(git_ident email)" ]]; then
    write_gitconfig "$name" "$email"
    info "commits in boxen projects: $name <$email>"
  else
    write_gitconfig "$(git_ident name)" "$(git_ident email)"
    info "keeping the commit identity already set ($(git_ident email)); your no-reply address is $email"
  fi

  ensure_token_timer || true

  info "minting tokens for existing projects"
  cmd_tokens || true
}

# repos NAME [--suggest | add REPO... | rm REPO...]: the GitHub repos
# the project's tokens cover (see the GitHub section above).
#   repos NAME            the list, each with its state, and the repos found in
#                         the project folder that aren't listed
#   repos NAME --suggest  just those found ones, one owner/repo per line
#   repos NAME add|rm     change the list (owner/repo or https://github.com/...)
#                         and mint the tokens again at once
# Only you run this: a project's agent can't (its shell can't write
# boxen's folder).
cmd_repos() {
  local usage="usage: boxen repos <name> [--suggest | add <owner/repo>... | rm <owner/repo>...]"
  require_project "${1:-}"
  local n=$1
  case ${2:-} in
    "")        (( $# == 1 )) || die "$usage"; repos_show "$n" ;;
    --suggest) (( $# == 2 )) || die "$usage"; unlisted_repos "$n" ;;
    add|rm)    repos_change "$@" ;;
    *)         die "$usage" ;;
  esac
}

# repos_show NAME: the list with each repo's state (from the last token
# refresh), the origin marked, then what's in the folder but not listed.
repos_show() {
  local n=$1 origin i r mark line repos=() states=() found=() bad=()
  mapfile -t repos < <(listed_repos "$n")
  mapfile -t states < <(repo_states "$n" "${repos[@]}")
  mapfile -t bad < <(bad_repo_lines "$n")
  origin=$(origin_repo "$n")
  echo "$n: GitHub repos (its tokens cover only these):"
  (( ${#repos[@]} )) || echo "    none (add one: boxen repos $n add owner/repo)"
  for i in "${!repos[@]}"; do
    r=${repos[i]}
    mark=""
    [[ "${r,,}" != "${origin,,}" ]] || mark="  (origin)"
    printf '    %-40s %s%s\n' "$r" "$(repo_state_text "${states[i]:--}")" "$mark"
  done
  for line in "${bad[@]}"; do
    printf '    ignored (not owner/repo): %s\n' "$line"
  done
  if [[ -n "$origin" && " ${repos[*],,} " != *" ${origin,,} "* ]]; then
    echo "$n: origin $origin isn't listed: pushes to it won't work"
  fi
  mapfile -t found < <(unlisted_repos "$n")
  if (( ${#found[@]} )); then
    echo "Found in the project folder but not listed:"
    printf '    %s\n' "${found[@]}"
    echo "  to add: boxen repos $n add <owner/repo>"
  fi
}

# repos_change NAME add|rm ARG...
repos_change() {
  local n=$1 op=$2 a r seen=" " listed want=() keep=() missing=() line
  shift 2
  for a in "$@"; do
    case $a in
      --yes|-y) ;;    # accepted for old scripts; nothing to confirm
      -*) die "unknown option '$a'" ;;
      *)
        r=$(repo_arg "$a") || die "'$a' isn't a GitHub repo (owner/repo or https://github.com/owner/repo)"
        [[ "$seen" == *" ${r,,} "* ]] || { want+=("$r"); seen+="${r,,} "; } ;;
    esac
  done
  (( ${#want[@]} )) || die "usage: boxen repos $n $op <owner/repo>..."
  lock_project "$n"
  seed_repos "$n"
  listed=" $(listed_repos "$n" | tr '\n' ' ') "
  listed=${listed,,}
  if [[ "$op" == add ]]; then
    for r in "${want[@]}"; do
      [[ "$listed" == *" ${r,,} "* ]] || missing+=("$r")
    done
    if (( ${#missing[@]} == 0 )); then
      # Asked again, most likely after fixing something on GitHub (made the
      # repo, added it to the app's installation): check now rather than at
      # the next timer run.
      echo "$n: already listed: ${want[*]}; checking GitHub again"
      if app_configured; then refresh_project "$n" || true; fi
      repos_show "$n"
      return 0
    fi
    # GitHub mints a token for at most 500 repos.
    local -A per=()
    while IFS= read -r r; do
      a=${r%%/*}
      per[${a,,}]=$(( ${per[${a,,}]:-0} + 1 ))
      (( per[${a,,}] <= 500 )) || die "$a: at most 500 repos per owner"
    done < <(listed_repos "$n"; printf '%s\n' "${missing[@]}")
    { cat "$(repos_file "$n")"; printf '%s\n' "${missing[@]}"; } | write_repos "$n"
    echo "$n: added ${missing[*]}"
  else
    while IFS= read -r line || [[ -n "$line" ]]; do
      r=${line%%#*}
      r=${r//[[:space:]]/}
      r=${r%.git}
      if [[ -n "$r" && "$seen" == *" ${r,,} "* ]]; then continue; fi
      keep+=("$line")
    done < "$(repos_file "$n")"
    for r in "${want[@]}"; do
      [[ "$listed" == *" ${r,,} "* ]] || warn "$n: $r isn't listed"
    done
    printf '%s\n' "${keep[@]}" | write_repos "$n"
    echo "$n: removed ${want[*]}"
  fi
  if app_configured; then
    refresh_project "$n" || true
  else
    warn "GitHub isn't set up yet ('boxen github'): no tokens until it is"
  fi
  repos_show "$n"
}
