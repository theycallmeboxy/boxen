"""Shared fakes and fixtures for boxen v2's tests.

Everything runs against a temp HOME (with BOXEN_HOME and BOXEN_SRC under
it) and fake commands first on PATH: docker (a rootless daemon, compose,
and a v1 system daemon behind -H), tmux, curl (the GitHub API), git (clone
only; everything else is the real git), timedatectl, systemctl, crontab and
claude. Each fake logs what it was asked to $FAKE_DIR. Nothing touches the
real ~ (checked at the end of the session).
"""
import fcntl
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOXEN = os.path.join(REPO, "bin", "boxen")
REAL_GIT = shutil.which("git")
DOCKER_HOST = "unix:///nonexistent/boxen-test-docker.sock"
FORMAT_MARK = "# boxen-format: 2"

# ------------------------------------------------------------------ fakes

FAKE_DOCKER = r'''#!PYTHON
"""Fake docker. State in $FAKE_DIR/docker.json:
  rootless: bool; containers: {name: {project, state, status, image, logs}};
  images: [names]; volumes: [names]; compose_config: JSON doc | null
  (what `compose config` in the throwaway container prints), compose_error;
  fail: [verbs] (compose verbs that fail: build, up, down);
  v1: {volumes: {name: {relpath: content}}}  (the system daemon, -H unix://...)
Every call is logged to docker.jsonl as {"argv", "host"}."""
import io, json, os, sys, tarfile
d = os.environ["FAKE_DIR"]
a = sys.argv[1:]
log = {"argv": a, "host": os.environ.get("DOCKER_HOST")}
sp = os.path.join(d, "docker.json")
st = json.load(open(sp))
def save():
    json.dump(st, open(sp, "w"), indent=1)
def done(rc=0, out=None, err=None):
    with open(os.path.join(d, "docker.jsonl"), "a") as f:
        f.write(json.dumps(log) + "\n")
    if out is not None:
        sys.stdout.write(out)
    if err is not None:
        sys.stderr.write(err)
    sys.exit(rc)
def opt_vals(args, name):
    return [args[i + 1] for i, x in enumerate(args[:-1]) if x == name]

if a[:1] == ["-H"]:
    # the system daemon (boxen v1's)
    log["v1"] = True
    v1 = st.get("v1")
    if v1 is None:
        done(1, err="Cannot connect to the Docker daemon at %s\n" % a[1])
    b = a[2:]
    if b[:1] == ["info"]:
        done(0, "")
    if b[:2] == ["image", "inspect"]:
        done(0 if b[2] in v1.get("images", ["boxen-base:latest"]) else 1)
    if b[:1] in (["stop"], ["pull"]):
        done(0)
    if b[:1] == ["ps"]:
        done(0, "")
    if b[:2] == ["volume", "inspect"]:
        done(0 if b[2] in v1.get("volumes", {}) else 1)
    if b[:2] == ["volume", "ls"]:
        flt = opt_vals(b, "--filter")
        names = sorted(v1.get("volumes", {}))
        import re
        for f in flt:
            if f.startswith("name="):
                names = [n for n in names if re.search(f[5:], n)]
        done(0, "".join(n + "\n" for n in names))
    if b[:1] == ["run"]:
        vol = [v.split(":")[0] for v in opt_vals(b, "-v")][0]
        files = v1.get("volumes", {}).get(vol, {})
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            for rel, content in sorted(files.items()):
                data = content.encode()
                ti = tarfile.TarInfo("./" + rel)
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
        log["tar"] = vol
        with open(os.path.join(d, "docker.jsonl"), "a") as f:
            f.write(json.dumps(log) + "\n")
        sys.stdout.buffer.write(buf.getvalue())
        sys.exit(0)
    done(1, err="fake v1 docker: unhandled %r\n" % b)

if st.get("down"):
    done(1, err="Cannot connect to the Docker daemon at %s. Is the docker daemon running?\n"
         % os.environ.get("DOCKER_HOST"))
cs = st.setdefault("containers", {})

if a[:1] == ["info"]:
    f = a[2] if len(a) > 2 else ""
    if f == "{{json .SecurityOptions}}":
        so = ["name=seccomp,profile=builtin", "name=cgroupns"]
        if st.get("rootless", True):
            so.append("name=rootless")
        done(0, json.dumps(so) + "\n")
    if "ClientInfo.Plugins" in f:
        done(0, os.path.join(d, "docker-compose") + "\n")
    if f == "{{.CgroupDriver}}":
        done(0, "systemd\n")
    done(0, "")

if a[:1] == ["compose"]:
    if a[1:2] == ["version"]:
        done(0, "Docker Compose version v5.5.0\n")
    pdir = a[a.index("--project-directory") + 1]
    n = os.path.basename(pdir)
    files = opt_vals(a, "-f")
    i = len(a) - 1
    rest = a[max(i for i, x in enumerate(a) if x in ("-f",)) + 2:]
    verb = rest[0]
    log["compose"] = {"project": n, "files": files, "args": rest}
    if verb in st.get("fail", []):
        done(1, err="fake compose: %s failed\n" % verb)
    if verb == "up":
        cs["boxen.%s.env" % n] = {"project": "boxen-" + n, "state": "running", "status": "Up 1 second",
                                  "image": "boxen-env-%s:latest" % n}
        sc = os.path.join(pdir, "compose.services.yaml")
        if os.path.exists(sc):
            for s in json.load(open(sc))["services"]:
                cs["boxen.%s.%s" % (n, s)] = {"project": "boxen-" + n, "state": "running",
                                              "status": "Up 1 second", "image": "x"}
        save()
    if verb == "build":
        st.setdefault("images", []).append("boxen-env-%s:latest" % n)
        save()
    if verb == "down":
        for c in [c for c, v in cs.items() if v.get("project") == "boxen-" + n]:
            del cs[c]
        save()
    done(0)

if a[:2] == ["image", "inspect"]:
    done(0 if a[2] in st.get("images", []) else 1, err=None)
if a[:1] == ["inspect"]:
    if a[1:2] == ["-f"]:
        c = cs.get(a[3])
        if c is None:
            done(1, err="Error: No such object: %s\n" % a[3])
        done(0, ("true" if c["state"] == "running" else "false") + "\n")
    done(0 if a[1] in cs else 1)
if a[:1] == ["ps"]:
    flt = opt_vals(a, "--filter")
    fmt = opt_vals(a, "--format")[0]
    out = []
    for name, c in sorted(cs.items()):
        ok = True
        for f in flt:
            if f == "label=com.docker.compose.project":
                ok = ok and bool(c.get("project"))
            elif f.startswith("label=com.docker.compose.project="):
                ok = ok and c.get("project") == f.split("=", 2)[2]
        if not ok:
            continue
        line = (fmt.replace('{{.Label "com.docker.compose.project"}}', c.get("project", ""))
                .replace("{{.Names}}", name).replace("{{.State}}", c["state"])
                .replace("{{.Status}}", c["status"]).replace("{{.Image}}", c.get("image", "")))
        out.append(line + "\n")
    done(0, "".join(out))
if a[:1] == ["logs"]:
    c = cs.get(a[-1])
    if c is None:
        done(1, err="Error: No such container: %s\n" % a[-1])
    done(0, c.get("logs", ""))
if a[:1] == ["exec"]:
    # docker exec [-i|-it] [-w DIR] [-e K=V]... CONTAINER CMD...: with
    # $FAKE_DIR/exec-local, CMD runs here (in DIR), so its argv can be checked.
    i, wd, envs = 1, None, {}
    while a[i].startswith("-"):
        if a[i] == "-w":
            wd = a[i + 1]; i += 2
        elif a[i] == "-e":
            k, _, v = a[i + 1].partition("="); envs[k] = v; i += 2
        else:
            i += 1
    if os.path.exists(os.path.join(d, "exec-local")):
        # boxen-exec is the image's (image/boxen-exec, under /bin/sh), its
        # run ids kept in $FAKE_DIR/exec
        with open(os.path.join(d, "docker.jsonl"), "a") as f:
            f.write(json.dumps(log) + "\n")
        cmd = a[i + 1:]
        if cmd[:1] == ["boxen-exec"]:
            cmd = ["/bin/sh", "REPODIR/image/boxen-exec"] + cmd[1:]
            envs["BOXEN_EXEC_DIR"] = os.path.join(d, "exec")
        if wd:
            os.chdir(wd)
        os.execvpe(cmd[0], cmd, dict(os.environ, **envs))
    done(0)
if a[:1] == ["run"]:
    if "/compose" in a:
        # compose config in a throwaway container: record what it was given
        w = [v for v in opt_vals(a, "-v") if v.endswith(":/w:ro")][0][:-len(":/w:ro")]
        log["services_yaml"] = open(os.path.join(w, "services.yaml")).read()
        log["w_files"] = sorted(os.listdir(w))
        if st.get("compose_error"):
            done(1, err=st["compose_error"] + "\n")
        done(0, json.dumps(st.get("compose_config") or {}) + "\n")
    if "-i" in a and "tar" in a:
        data = sys.stdin.buffer.read()
        log["stdin_bytes"] = len(data)
        try:
            with tarfile.open(fileobj=io.BytesIO(data)) as t:
                log["stdin_members"] = sorted(m.name for m in t.getmembers())
        except tarfile.TarError:
            pass
        done(0)
    done(0)
if a[:1] in (["build"], ["tag"], ["builder"]) or a[:2] in (["image", "prune"], ["image", "rm"]):
    if a[:1] == ["tag"]:
        st.setdefault("images", []).append(a[2])
        save()
    done(0)
if a[:1] == ["volume"]:
    vols = st.setdefault("volumes", [])
    if a[1] == "inspect":
        done(0 if a[2] in vols else 1)
    if a[1] == "create":
        vols.append(a[-1]); save(); done(0, a[-1] + "\n")
    if a[1] == "ls":
        done(0, "")
    if a[1] == "rm":
        done(0)
done(1, err="fake docker: unhandled %r\n" % a)
'''

FAKE_TMUX = r'''#!PYTHON
"""Fake tmux: argv to tmux.jsonl (with DOCKER_HOST, which must be unset);
a session while $FAKE_DIR/tmux-session exists; window names in
tmux-windows.json."""
import json, os, sys
d = os.environ["FAKE_DIR"]
argv = sys.argv[1:]
with open(os.path.join(d, "tmux.jsonl"), "a") as f:
    f.write(json.dumps({"argv": argv, "docker_host": os.environ.get("DOCKER_HOST")}) + "\n")
wp = os.path.join(d, "tmux-windows.json")
wins = json.load(open(wp)) if os.path.exists(wp) else []
args = argv[2:] if argv[:1] == ["-L"] else argv
if args[:2] == ["-f", args[1] if len(args) > 1 else ""]:
    args = args[2:]
cmd = args[0] if args else ""
def opt(n):
    return args[args.index(n) + 1] if n in args else None
has = os.path.exists(os.path.join(d, "tmux-session"))
if cmd == "-V":
    print("tmux 3.4"); sys.exit(0)
if cmd == "has-session":
    sys.exit(0 if has else 1)
if cmd == "list-windows":
    if not has:
        sys.stderr.write("no server running\n"); sys.exit(1)
    for w in wins:
        print(w, flush=True)       # a line at a time, as a tmux client may write
    sys.exit(0)
if cmd in ("new-window", "new-session"):
    open(os.path.join(d, "tmux-session"), "w").close()
    wins.append(opt("-n"))
    json.dump(wins, open(wp, "w"))
    sys.exit(0)
if cmd == "kill-window":
    t = opt("-t")
    wins = [w for w in wins if "boxen:=" + w != t]
    json.dump(wins, open(wp, "w"))
    sys.exit(0)
sys.exit(0)
'''

FAKE_CURL = r'''#!PYTHON
"""The GitHub API, as gh_app_api calls it (curl -s --fail-with-body -X METHOD
... [-d JSON] URL), from $FAKE_DIR/github.json: {"installations": [{"login",
"id", "repos": [names] | "all"}], "down": bool}. Calls go to curl.jsonl."""
import json, os, re, sys
d = os.environ["FAKE_DIR"]
a = sys.argv[1:]
method, body, url, i = "GET", None, a[-1], 0
while i < len(a) - 1:
    if a[i] in ("-X", "-d"):
        if a[i] == "-X": method = a[i + 1]
        else: body = a[i + 1]
        i += 2
        continue
    i += 1
with open(os.path.join(d, "curl.jsonl"), "a") as f:
    f.write(json.dumps({"method": method, "url": url, "body": json.loads(body) if body else None}) + "\n")
gh = json.load(open(os.path.join(d, "github.json")))
if gh.get("down"):
    sys.exit(7)
path = url.split("https://api.github.com", 1)[1]
insts = gh.get("installations", [])

def out(obj, fail=False):
    sys.stdout.write(json.dumps(obj))
    sys.exit(22 if fail else 0)

def find(owner):
    return next((x for x in insts if x["login"].lower() == owner.lower()), None)

def can(x, repo):
    return x["repos"] == "all" or repo.lower() in [r.lower() for r in x["repos"]]

if path.startswith("/app/installations?"):
    out([{"id": x["id"], "account": {"login": x["login"]}} for x in insts])
m = re.match(r"^/repos/([^/]+)/([^/]+)/installation$", path)
if m:
    x = find(m.group(1))
    if x and can(x, m.group(2)):
        out({"id": x["id"]})
    out({"message": "Not Found"}, True)
m = re.match(r"^/app/installations/(\d+)/access_tokens$", path)
if m and method == "POST":
    x = [y for y in insts if str(y["id"]) == m.group(1)][0]
    names = json.loads(body)["repositories"]
    if not all(can(x, n) for n in names):
        out({"message": "There is at least one repository that does not exist or is not accessible to the "
                        "parent installation."}, True)
    out({"token": "tok-%s-%s" % (x["login"].lower(), ",".join(names)), "expires_at": "2026-10-19T12:00:00Z"})
out({"message": "fake: unhandled %s %s" % (method, path)}, True)
'''

FAKE_GIT = r'''#!PYTHON
"""git, except `clone`: logged to git.jsonl with the token the credential
helper (the last -c credential.https://github.com.helper) gives for the
URL, then a repo with that origin is made at the destination. Fails while
$FAKE_DIR/git-clone-fail exists."""
import json, os, subprocess, sys
d = os.environ["FAKE_DIR"]
a = sys.argv[1:]
if "clone" not in a:
    os.execv("REALGIT", ["git"] + a)
cfg = [a[i + 1] for i, x in enumerate(a[:-1]) if x == "-c"]
rest = a[a.index("clone") + 1:]
if rest[:1] == ["--"]:
    rest = rest[1:]
url, dest = rest[0], rest[1]
helpers = [c.split("=", 1)[1] for c in cfg if c.startswith("credential.https://github.com.helper=")]
cred = None
if helpers and helpers[-1].startswith("!"):
    path = url.split("https://github.com/", 1)[-1]
    r = subprocess.run(["sh", "-c", helpers[-1][1:] + " get"], capture_output=True, text=True,
                       input="protocol=https\nhost=github.com\npath=%s\n\n" % path)
    cred = r.stdout
with open(os.path.join(d, "git.jsonl"), "a") as f:
    f.write(json.dumps({"argv": a, "cwd": os.getcwd(), "cred": cred}) + "\n")
if os.path.exists(os.path.join(d, "git-clone-fail")):
    sys.stderr.write("fatal: repository not found\n")
    sys.exit(128)
subprocess.run(["REALGIT", "init", "-q", dest], check=True)
subprocess.run(["REALGIT", "-C", dest, "remote", "add", "origin", url], check=True)
'''

FAKE_CRONTAB = r'''#!/bin/sh
f="$FAKE_DIR/crontab"
case "$1" in
  -l) [ -f "$f" ] && cat "$f" ;;
  -) cat > "$f" ;;
esac
'''


def write_exe(path, content):
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, 0o755)


def py_exe(path, src):
    write_exe(str(path), src.replace("#!PYTHON", "#!" + sys.executable, 1).replace("REALGIT", REAL_GIT)
              .replace("REPODIR", REPO))


# ----------------------------------------------------------- the real ~

def _fingerprint():
    real = os.path.expanduser("~")
    out = {}
    for rel in (".gitconfig", ".claude/settings.json", ".claude/CLAUDE.md", ".claude/boxen.md", ".claude.json"):
        p = os.path.join(real, rel)
        try:
            with open(p, "rb") as f:
                out[rel] = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            out[rel] = None
    return out


@pytest.fixture(scope="session", autouse=True)
def real_home_untouched():
    before = _fingerprint()
    yield
    assert _fingerprint() == before, "a test changed files in the real home folder"


# ------------------------------------------------------------- fixtures

class VM:
    """A test VM: paths, env, and helpers to run boxen and read the fakes' logs."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.home = tmp / "home"
        self.bhome = self.home / ".config" / "boxen"
        self.projects = self.bhome / "projects"
        self.src = self.home / "src"
        self.fake = tmp / "fake"
        self.bin = tmp / "bin"
        self.run_dir = tmp / "run"
        for p in (self.projects, self.src, self.fake, self.bin, self.run_dir):
            p.mkdir(parents=True, exist_ok=True)
        self.state = {"rootless": True, "containers": {}, "images": ["boxen-env:latest", "boxen-env:core"],
                      "volumes": []}
        self.save_docker()
        py_exe(self.bin / "docker", FAKE_DOCKER)
        py_exe(self.bin / "tmux", FAKE_TMUX)
        py_exe(self.bin / "git", FAKE_GIT)
        write_exe(str(self.bin / "timedatectl"), "#!/bin/sh\necho Europe/Berlin\n")
        write_exe(str(self.bin / "systemctl"), "#!/bin/sh\nexit 1\n")
        write_exe(str(self.bin / "loginctl"), "#!/bin/sh\necho no\n")
        write_exe(str(self.bin / "crontab"), FAKE_CRONTAB)
        write_exe(str(self.bin / "claude"), "#!/bin/sh\necho 'claude (fake)' \"$@\"\n")
        write_exe(str(self.fake / "docker-compose"), "#!/bin/sh\nexit 0\n")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("BOXEN_", "XDG_", "GH_", "GITHUB_", "GIT_", "CLAUDE_", "TMUX", "DOCKER_"))}
        env.update(HOME=str(self.home), BOXEN_HOME=str(self.bhome), BOXEN_SRC=str(self.src),
                   BOXEN_DOCKER_HOST=DOCKER_HOST, XDG_RUNTIME_DIR=str(self.run_dir), FAKE_DIR=str(self.fake),
                   PATH=str(self.bin) + os.pathsep + os.environ["PATH"], TERM="xterm-test",
                   GIT_CONFIG_NOSYSTEM="1", TMPDIR=str(tmp / "tmpdir"))
        (tmp / "tmpdir").mkdir()
        self.env = env

    # docker state
    def save_docker(self):
        (self.fake / "docker.json").write_text(json.dumps(self.state, indent=1))

    def load_docker(self):
        self.state = json.loads((self.fake / "docker.json").read_text())
        return self.state

    def set_docker(self, **kw):
        self.load_docker()
        self.state.update(kw)
        self.save_docker()

    def container(self, name, project, state="running", status="Up 2 hours", **kw):
        self.load_docker()
        self.state["containers"][name] = dict(project=project, state=state, status=status, **kw)
        self.save_docker()

    # logs
    def _jsonl(self, name, clear=False):
        p = self.fake / name
        out = [json.loads(line) for line in open(p)] if p.exists() else []
        if clear and p.exists():
            p.unlink()
        return out

    def docker_log(self, clear=False):
        return self._jsonl("docker.jsonl", clear)

    def docker_calls(self, clear=False):
        return [c["argv"] for c in self.docker_log(clear)]

    def compose_calls(self):
        return [c["compose"] for c in self.docker_log() if "compose" in c]

    def tmux_calls(self, clear=False):
        return [c["argv"] for c in self._jsonl("tmux.jsonl", clear)]

    def git_log(self):
        return self._jsonl("git.jsonl")

    def clear_logs(self):
        for n in ("docker.jsonl", "tmux.jsonl", "git.jsonl", "curl.jsonl"):
            (self.fake / n).unlink(missing_ok=True)

    # paths
    def pdir(self, n):
        return self.projects / n

    # projects
    def make_project(self, name, v2=True, src=True, origin=None, boxen_dir=True):
        p = self.pdir(name)
        p.mkdir(parents=True, exist_ok=True)
        head = FORMAT_MARK + "\n" if v2 else ""
        (p / "compose.yaml").write_text(head + "name: boxen-%s\nservices:\n  env: {}\n" % name)
        if src:
            s = self.src / name
            s.mkdir(parents=True, exist_ok=True)
            if origin:
                subprocess.run([REAL_GIT, "init", "-q", str(s)], check=True)
                subprocess.run([REAL_GIT, "-C", str(s), "remote", "add", "origin", origin], check=True)
            if boxen_dir and v2:
                (s / ".boxen").mkdir(exist_ok=True)
                (s / ".boxen" / "Dockerfile").write_text("FROM boxen-env:latest\n")
                (s / ".boxen" / "services.yaml").write_text("# nothing\nservices:\n")
        return p

    # running boxen
    def boxen(self, *args, cwd=None, env=None, input=None, timeout=60):
        e = dict(self.env, **(env or {}))
        return subprocess.run([BOXEN] + [str(a) for a in args], env=e, cwd=cwd, capture_output=True, text=True,
                              input=input, timeout=timeout, stdin=None if input is not None else subprocess.DEVNULL)

    def ok(self, *args, **kw):
        r = self.boxen(*args, **kw)
        assert r.returncode == 0, "boxen %s: rc %d\nstdout:\n%s\nstderr:\n%s" % (
            " ".join(map(str, args)), r.returncode, r.stdout, r.stderr)
        return r

    def fn(self, script, env=None, cwd=None, input=None):
        """Run bash with bin/boxen's functions loaded (everything above its
        main section) and then SCRIPT."""
        return run_fn(script, dict(self.env, **(env or {})), cwd=cwd, input=input)


def boxen_functions():
    """bin/boxen without its main section, with BOXEN_BIN fixed (so it can
    be sourced from a pipe)."""
    text = open(BOXEN).read()
    cut = text.index("\n# ------------------------------------------------------------------- main\n")
    text = text[:cut].replace('BOXEN_BIN="$(readlink -f "${BASH_SOURCE[0]}")"', 'BOXEN_BIN=%s' % BOXEN, 1)
    assert "BOXEN_BIN=" + BOXEN in text
    return text


def run_fn(script, env, cwd=None, input=None):
    prog = boxen_functions() + "\n" + script + "\n"
    return subprocess.run(["bash", "-c", prog], env=env, cwd=cwd, capture_output=True, text=True, input=input,
                          timeout=60)


@pytest.fixture
def vm(tmp_path):
    """alpha (v2, running, a db sidecar, origin theycallmeboxy/alpha), beta
    (v2, never started) and old (v1)."""
    v = VM(tmp_path)
    v.make_project("alpha", origin="https://github.com/theycallmeboxy/alpha.git")
    v.make_project("beta")
    v.make_project("old", v2=False)
    (v.pdir("alpha") / "compose.services.yaml").write_text(json.dumps(
        {"services": {"db": {"image": "postgres:16", "container_name": "boxen.alpha.db"}},
         "volumes": {}}))
    v.container("boxen.alpha.env", "boxen-alpha", logs="")
    v.container("boxen.alpha.db", "boxen-alpha", logs="db ready\n")
    v.load_docker()
    v.state["images"].append("boxen-env-alpha:latest")
    v.save_docker()
    return v


def hold_lock(path):
    f = open(path, "a")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


def wait_for(cond, timeout=10.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


def unix_socket(path):
    """A listening unix socket at path (for a check that one is there)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.listen(1)
    return s


def group_members(pgid):
    """Live (not zombie) processes in process group pgid."""
    out = []
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            stat = open("/proc/%s/stat" % p).read()
        except OSError:
            continue
        fields = stat.rsplit(")", 1)[1].split()
        if int(fields[2]) == pgid and fields[0] != "Z":
            out.append(int(p))
    return out
