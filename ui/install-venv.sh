#!/usr/bin/env bash
# boxen ui's Python environment: a venv with exactly ui/requirements.lock
# (every package pinned and hash-checked, wheels only). Run by install.sh
# (and so by self-update).
#
# Each version lives in its own folder, ui-venv-<first 12 of the lock's
# sha256>, and ui-venv is a link to the current one, switched atomically
# once the new one is complete. A running dashboard was started from the
# folder itself (bin/boxen resolves the link), so it keeps its version: the
# previous folder stays until the next install. Nothing to do when the link
# already points at a complete venv for this lock.
set -euo pipefail

UI_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
LINK="${BOXEN_UI_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/boxen/ui-venv}"
LOCK="$UI_DIR/requirements.lock"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

hash=$(sha256sum "$LOCK" | cut -d' ' -f1)
VENV="$LINK-${hash:0:12}"
NAME=${VENV##*/}
complete() { [[ -x "$VENV/bin/python" && "$(cat "$VENV/.boxen-lock" 2>/dev/null || true)" == "$hash" ]]; }
fail() { printf 'install: %s\n' "$*" >&2; complete || rm -rf "$VENV"; exit 1; }

if complete && [[ -L "$LINK" && "$(readlink "$LINK")" == "$NAME" ]]; then
  info "boxen ui: Python environment up to date ($VENV)"
  exit 0
fi

mkdir -p "$(dirname "$LINK")"
if ! complete; then
  info "boxen ui: installing its Python environment ($VENV)"
  rm -rf "$VENV"
  python3 -m venv "$VENV" >/dev/null 2>&1 \
    || fail "couldn't create a Python venv for boxen ui (is python3-venv installed?)"
  "$VENV/bin/python" -m pip install --disable-pip-version-check -q --require-hashes \
      --only-binary=:all: --timeout 20 --retries 1 -r "$LOCK" \
    || fail "couldn't install boxen ui's Python packages ($LOCK); it needs pypi.org and files.pythonhosted.org"
  PYTHONPATH="$UI_DIR" "$VENV/bin/python" -P -m boxen_ui --check >/dev/null \
    || fail "boxen ui's packages installed, but it doesn't start ($VENV/bin/python -m boxen_ui --check)"
  printf '%s\n' "$hash" > "$VENV/.boxen-lock"
fi

# The folder in use until now stays for whatever dashboard runs from it.
prev=""
if [[ -L "$LINK" ]]; then
  prev=$(readlink "$LINK")
elif [[ -d "$LINK" ]]; then
  # From before versioned folders: a plain folder; moved aside like any old version.
  rm -rf "$LINK-legacy"
  mv "$LINK" "$LINK-legacy"
  prev="${LINK##*/}-legacy"
fi
ln -sfn "$NAME" "$LINK.tmp.$$"
mv -T "$LINK.tmp.$$" "$LINK"
for d in "$LINK"-*; do
  [[ -d "$d" && ! -L "$d" ]] || continue
  case ${d##*/} in
    "$NAME"|"$prev") ;;
    *) rm -rf "$d" ;;
  esac
done
rm -f "$LINK.sha256"
info "boxen ui: ready (boxen ui)"
