"""Editing a project's file (Files tab, e) without trusting its path.

Some of the files live where the project's agent writes (~/src/<name>/.boxen/),
so the editor never gets the real path: the dashboard reads the file without
following links (files.py), byte for byte, into a private folder (0700:
in $XDG_RUNTIME_DIR, which the sandbox hides, or ~/.local/state/boxen/ui/edit;
never /tmp, which agents can reach) and opens the editor on that copy in a
tmux window, which runs

    python -P -m boxen_ui.edit <private folder>/spec.json <its sha256>

spec.json says what to run and where to write; it's acted on only if it's
still what the dashboard wrote (the hash comes on the command line).

That runs the editor (vim with modelines off: a file can't set options),
and when it exits writes the copy back: to a temp file next to the target,
then a rename over the target, which replaces a link put there meanwhile
rather than following it. Every folder on the way below the project's own
folder is opened without following links. Only when the content changed,
and only if the target is still a plain file with the content the copy
started from (else the copy is kept and the window says where). The outcome
goes to result.json in the private folder for the dashboard.
"""
import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
import tempfile

from . import files

LIMIT = 1024 * 1024     # a bigger file isn't edited here (refused up front)
VIM_NAMES = ("vim", "vi", "nvim", "view", "vim.basic", "vim.tiny", "vim.nox", "vim.gtk3")


def _sha(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _own_dir(path, private=False):
    """path is a folder (not a link) of yours that nobody else can write
    (with private, nobody else can enter either)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return (stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()
            and not st.st_mode & (0o077 if private else 0o022))


def private_dir(env=None):
    """A new 0700 folder for one edit: in $XDG_RUNTIME_DIR when that's
    usable, else in ~/.local/state/boxen/ui/edit (made 0700). Never in
    /tmp. Raises OSError when neither will do."""
    env = os.environ if env is None else env
    base = env.get("XDG_RUNTIME_DIR") or ""
    if not (base.startswith("/") and _own_dir(base)):
        home = env.get("HOME") or os.path.expanduser("~")
        state = env.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state")
        base = os.path.join(state, "boxen", "ui", "edit")
        os.makedirs(base, mode=0o700, exist_ok=True)
        if not _own_dir(base, private=True):
            if _own_dir(base):          # a folder of yours, not a link: just too open
                os.chmod(base, 0o700)
            if not _own_dir(base, private=True):
                raise OSError("%s isn't a private folder of yours (0700); nowhere safe for the copy" % base)
    return tempfile.mkdtemp(prefix="boxen-ui-edit-", dir=base)      # mode 0700


def spec_sha(path):
    with open(path, "rb") as f:
        return _sha(f.read())


def prepare(project, label, base, rel, editor, env=None, template=None):
    """Copy base/rel... (rel: the path's parts below base, a trusted folder)
    byte for byte into a private folder; returns the spec (a dict, also
    written to spec.json there, its hash in spec["sha"]). A missing file's
    copy starts as template (bytes) or empty. Raises files.Refused (TooBig
    over LIMIT) or OSError."""
    data = files.read_bytes_under(base, rel, limit=LIMIT)
    start = data if data is not None else (template or b"")
    d = private_dir(env)
    copy = os.path.join(d, rel[-1])
    fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(start)
    spec = {"project": project, "label": label, "base": base, "rel": list(rel),
            "target": os.path.join(base, *rel), "copy": copy, "orig_sha": _sha(data), "start_sha": _sha(start),
            "editor": list(editor), "dir": d, "result": os.path.join(d, "result.json")}
    raw = json.dumps(spec).encode("utf-8")
    fd = os.open(os.path.join(d, "spec.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    spec["sha"] = _sha(raw)
    return spec


def editor_cmd(editor, copy):
    argv = list(editor)
    if os.path.basename(argv[0]) in VIM_NAMES:
        argv += ["--cmd", "set nomodeline"]
    return argv + [copy]


def _unlink_if(name, dfd, st):
    """Remove our temp file, if name still is it."""
    try:
        cur = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        if st is None or (cur.st_dev, cur.st_ino) == (st.st_dev, st.st_ino):
            os.unlink(name, dir_fd=dfd)
    except OSError:
        pass


def write_back(spec, new):
    """Put new (bytes) in place of spec's target. Returns {"status":
    unchanged|written|conflict|refused, "message"}."""
    if _sha(new) in (spec["start_sha"], spec["orig_sha"]) or (spec["orig_sha"] is None and new == b""):
        return {"status": "unchanged", "message": "no changes"}
    target = spec["target"]
    name = spec["rel"][-1]
    try:
        dfd = files.open_dir(spec["base"], spec["rel"][:-1])
    except (OSError, files.Refused) as e:
        return {"status": "refused", "message": "%s: its folder is a symlink or gone (%s)"
                % (target, getattr(e, "strerror", None) or e)}
    tfd = None
    try:
        mode = 0o644
        try:
            st = os.lstat(name, dir_fd=dfd)
        except FileNotFoundError:
            st = None
        if st is not None:
            if not stat.S_ISREG(st.st_mode):
                return {"status": "refused", "message": "%s is now a symlink or not a file; not written" % target}
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
            with os.fdopen(fd, "rb") as f:
                cur = f.read()
            mode = stat.S_IMODE(st.st_mode)
            if _sha(cur) != spec["orig_sha"]:
                return {"status": "conflict", "message": "%s changed while you edited it; not written" % target}
        elif spec["orig_sha"] is not None:
            return {"status": "conflict", "message": "%s was removed while you edited it; not written" % target}
        # The temp file goes next to the target, where the agent may write
        # too. So it's never touched by path once created: created
        # exclusively without following links, mode set on the open file,
        # and before it's renamed into place, the name must still be that
        # very file (device and inode); afterwards the target must be it.
        tfd = os.dup(dfd)
        tmp = ".boxen-edit-%s" % secrets.token_hex(6)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                     0o600, dir_fd=tfd)
        mine = None
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(new)
                f.flush()
                os.fsync(f.fileno())
                os.fchmod(f.fileno(), mode)
                mine = os.fstat(f.fileno())
            try:
                there = os.stat(tmp, dir_fd=tfd, follow_symlinks=False)
            except FileNotFoundError:
                there = None
            if there is None or (there.st_dev, there.st_ino) != (mine.st_dev, mine.st_ino):
                return {"status": "refused", "message": "%s: the file being written changed during the write; "
                        "not written" % target}
            os.replace(tmp, name, src_dir_fd=tfd, dst_dir_fd=dfd)
        except BaseException:
            _unlink_if(tmp, tfd, mine)
            raise
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (mine.st_dev, mine.st_ino):
            return {"status": "refused", "message": "%s changed as it was written (it isn't the file written); "
                    "check it" % target}
        return {"status": "written", "message": "saved %s" % target}
    except OSError as e:
        return {"status": "refused", "message": "%s: %s; not written" % (target, e.strerror or e)}
    finally:
        os.close(dfd)
        if tfd is not None:
            os.close(tfd)


def main(argv):
    if len(argv) != 2:
        print("boxen ui: usage: python -m boxen_ui.edit SPEC SHA256 (restart the dashboard if it's older)")
        return 2
    with open(argv[0], "rb") as f:
        raw = f.read()
    if _sha(raw) != argv[1]:
        print("boxen ui: %s changed after the dashboard wrote it; not acting on it" % argv[0])
        return 1
    spec = json.loads(raw)
    try:
        rc = subprocess.call(editor_cmd(spec["editor"], spec["copy"]))
    except OSError as e:
        rc = 127
        print("boxen ui: can't run the editor %s: %s" % (spec["editor"][0], e.strerror or e))
    with open(spec["copy"], "rb") as f:
        new = f.read()
    res = write_back(spec, new) if rc == 0 else {
        "status": "conflict", "message": "the editor exited with %d; nothing written" % rc}
    if res["status"] in ("conflict", "refused") and _sha(new) != spec["orig_sha"]:
        res["message"] += "; your edit is kept at %s" % spec["copy"]
        res["kept"] = spec["copy"]
    print(res["message"])
    tmp = spec["result"] + ".tmp"
    with open(tmp, "w") as f:
        json.dump(res, f)
    os.replace(tmp, spec["result"])
    return 0 if res["status"] in ("written", "unchanged") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
