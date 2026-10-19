"""The smaller programs: bin/boxen-gh (gh with the project's token),
image/boxen-bg (background processes in the build container),
bin/boxen-services (the sidecar allowlist), and static checks
(shellcheck, bash -n) for every shell script."""
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

from conftest import REPO, group_members, write_exe, wait_for

# ------------------------------------------------------------------ boxen-gh

GH_SRC = os.path.join(REPO, "bin", "boxen-gh")


@pytest.fixture
def ghw(vm):
    """boxen-gh with its real gh (/usr/bin/gh) swapped for a fake that
    prints what it got. The copy changes nothing else in the script."""
    fake = vm.tmp / "gh-real"
    write_exe(str(fake), '#!/bin/sh\nprintf "%s|%s|%s|%s|" "${GH_TOKEN-unset}" "${GH_CONFIG_DIR-unset}" '
                         '"${GITHUB_TOKEN-unset}" "$(pwd)"\nprintf "%s|" "$@"\n')
    text = open(GH_SRC).read()
    assert text.count("real=/usr/bin/gh\n") == 1
    wrapper = vm.tmp / "gh"
    write_exe(str(wrapper), text.replace("real=/usr/bin/gh\n", "real=%s\n" % fake))
    tok = vm.pdir("alpha") / "token"
    tok.mkdir()
    (tok / "gh-token").write_text("DEFAULT\n")
    (tok / "gh-token.a").write_text("A\nsecond\n")
    (tok / "gh-token.b").write_text("B\n")

    def gh(*args, cwd=None, env=None):
        e = dict(vm.env, GH_TOKEN="MINE", GITHUB_TOKEN="ALSO-MINE", **(env or {}))
        r = subprocess.run([str(wrapper)] + list(args), capture_output=True, text=True,
                           cwd=cwd or vm.src / "alpha", env=e)
        assert r.returncode == 0, r.stderr
        f = r.stdout.split("|")
        return {"token": f[0], "config": f[1], "github_token": f[2], "cwd": f[3], "args": f[4:-1]}
    vm.gh = gh
    vm.gh_config = str(vm.tmp / "tmpdir" / ("boxen-gh-%d" % os.getuid()))
    return vm


def test_gh_outside_a_project_is_plain_gh(ghw):
    (ghw.src / "notaproject").mkdir()
    for cwd in (ghw.tmp, ghw.src, ghw.src / "old", ghw.src / "notaproject"):
        r = ghw.gh("pr", "list", "-R", "a/x", cwd=cwd)
        assert r == {"token": "MINE", "config": "unset", "github_token": "ALSO-MINE", "cwd": str(cwd),
                     "args": ["pr", "list", "-R", "a/x"]}, cwd


def test_gh_in_a_project_uses_its_token(ghw):
    gh = ghw.gh
    r = gh("pr", "list")                         # origin theycallmeboxy/alpha: no token of that owner
    assert r["token"] == "DEFAULT" and r["github_token"] == "unset" and r["args"] == ["pr", "list"]
    assert r["config"] == ghw.gh_config
    assert oct(os.stat(ghw.gh_config).st_mode & 0o777) == "0o700"
    assert os.listdir(ghw.gh_config) == []
    subprocess.run(["git", "-C", str(ghw.src / "alpha"), "remote", "set-url", "origin", "git@github.com:B/repo.git"],
                   check=True)
    assert gh("pr", "list")["token"] == "B"                                    # the origin's owner
    sub = ghw.src / "alpha" / "sub"
    sub.mkdir()
    assert gh("pr", "list", cwd=sub)["token"] == "B"
    assert gh("pr", "list", "-R", "a/x")["token"] == "A"
    assert gh("-R", "https://github.com/A/x", "issue", "list")["token"] == "A"
    assert gh("pr", "view", "--repo=a/x")["token"] == "A"
    assert gh("pr", "view", "-Ra/x")["token"] == "A"
    assert gh("pr", "view", "--repo", "c/x")["token"] == "DEFAULT"              # no token for c
    assert gh("pr", "view", env={"GH_REPO": "a/x"})["token"] == "A"
    r = gh("api", "--", "-R", "a/x")
    assert r["token"] == "B" and r["args"] == ["api", "--", "-R", "a/x"]       # after --: an argument
    assert gh("pr", "list", "-R", "gitlab.com/a/x")["token"] == "DEFAULT"
    assert gh("pr", "list", "-R", "../gh-token.a/x")["token"] == "DEFAULT"
    # through a link: the physical folder decides
    os.symlink(ghw.src / "alpha", ghw.tmp / "alink")
    assert gh("pr", "list", cwd=ghw.tmp / "alink")["token"] == "B"
    # no tokens at all: an empty token, never yours
    for f in (ghw.pdir("alpha") / "token").iterdir():
        f.unlink()
    r = gh("pr", "list")
    assert r["token"] == "" and r["config"] == ghw.gh_config
    r = gh("pr", "list", cwd=ghw.src / "beta")
    assert r["token"] == "" and r["github_token"] == "unset"


@pytest.mark.skipif(os.path.exists("/usr/bin/gh"), reason="gh is installed here")
def test_gh_without_real_gh(vm):
    r = subprocess.run([GH_SRC, "version"], capture_output=True, text=True, env=vm.env, cwd=vm.tmp)
    assert r.returncode == 127 and "gh: /usr/bin/gh not found" in r.stderr


# ----------------------------------------------------------------- boxen-bg

BG = os.path.join(REPO, "image", "boxen-bg")
EXEC = os.path.join(REPO, "image", "boxen-exec")

REAPER = r'''
"""A stand-in for the container's init: a child subreaper (so the
processes boxen-bg leaves behind are reaped when they exit, as tini does)
that runs commands sent as JSON lines on stdin and answers each with
{"rc", "out", "err"}."""
import ctypes, json, os, select, sys, tempfile
ctypes.CDLL(None).prctl(36, 1, 0, 0, 0)          # PR_SET_CHILD_SUBREAPER

def reap(until=None):
    while True:
        try:
            pid, status = os.waitpid(-1, 0 if until else os.WNOHANG)
        except ChildProcessError:
            return None
        if pid == 0:
            return None
        if pid == until:
            return os.waitstatus_to_exitcode(status)

buf = b""
while True:
    r, _, _ = select.select([0], [], [], 0.02)
    reap()
    if not r:
        continue
    chunk = os.read(0, 65536)
    if not chunk:
        break
    buf += chunk
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        req = json.loads(line)
        out, err = tempfile.TemporaryFile(), tempfile.TemporaryFile()
        pid = os.fork()
        if pid == 0:
            os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
            os.dup2(out.fileno(), 1)
            os.dup2(err.fileno(), 2)
            os.execvpe(req["argv"][0], req["argv"], req["env"])
        rc = reap(until=pid)
        out.seek(0); err.seek(0)
        sys.stdout.write(json.dumps({"rc": rc, "out": out.read().decode(), "err": err.read().decode()}) + "\n")
        sys.stdout.flush()
'''


class Reaper:
    def __init__(self, tmp, env):
        p = tmp / "reaper.py"
        p.write_text(REAPER)
        self.env = env
        self.proc = subprocess.Popen([sys.executable, str(p)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True)

    def run(self, *argv):
        self.proc.stdin.write(json.dumps({"argv": list(argv), "env": self.env}) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(10)


@pytest.fixture
def bg(tmp_path):
    d = tmp_path / "bg"
    env = dict(os.environ, BOXEN_BG_DIR=str(d))
    r = Reaper(tmp_path, env)
    r.dir = d
    r.bg = lambda *a: r.run("/bin/sh", BG, *a)        # the container's /bin/sh (dash on Ubuntu)
    yield r
    # whatever a failing test left running
    for f in d.glob("*.pid") if d.exists() else []:
        try:
            os.killpg(int(f.read_text()), 9)
        except (OSError, ValueError):
            pass
    r.close()


def test_bg_start_ps_logs_stop(bg):
    r = bg.bg("start", "web", "sh", "-c", "echo started; sleep 60 & sleep 60; wait")
    assert r["rc"] == 0, r["err"]
    pid = int((bg.dir / "web.pid").read_text())
    assert r["out"] == "web: started (pid %d); output: boxen run --logs web\n" % pid
    assert wait_for(lambda: len(group_members(pid)) >= 3)      # the shell, sh -c, two sleeps
    members = group_members(pid)
    r = bg.bg("ps")
    assert r["out"] == "%-20s %-22s %s\n" % ("web", "running (pid %d)" % pid,
                                            "sh -c echo started; sleep 60 & sleep 60; wait")
    r = bg.bg("start", "web", "true")
    assert r["rc"] == 1 and "web is already running (boxen run --stop web first)" in r["err"]
    assert wait_for(lambda: bg.bg("logs", "web")["out"] == "started\n")
    t = time.monotonic()
    r = bg.bg("stop", "web")
    assert r["rc"] == 0 and r["out"] == "web: stopped\n"
    assert time.monotonic() - t < 5                            # TERM did it, no 10 s wait for KILL
    # every process in the group is gone, not just the one in the pid file
    assert wait_for(lambda: group_members(pid) == [])
    for m in members:
        assert not os.path.exists("/proc/%d" % m) or open("/proc/%d/stat" % m).read().rsplit(")", 1)[1].split()[0] \
            == "Z"
    r = bg.bg("ps")
    assert r["out"].startswith("web") and "stopped" in r["out"]
    r = bg.bg("stop", "web")
    assert r["rc"] == 0 and r["out"] == "web: not running\n"


def test_bg_exit_code_and_logs(bg):
    r = bg.bg("start", "job", "sh", "-c", "echo one; echo two; echo err >&2; exit 3")
    assert r["rc"] == 0, r["err"]
    assert wait_for(lambda: (bg.dir / "job.rc").exists() and (bg.dir / "job.rc").read_text() == "3\n")
    assert "exited (3)" in bg.bg("ps")["out"]
    assert bg.bg("logs", "job")["out"] == "one\ntwo\nerr\n"
    assert bg.bg("logs", "job", "--tail", "1")["out"] == "err\n"
    for bad in (["--tail", "x"], ["--tail"], ["--tail", "1\n2"], ["-x"]):
        r = bg.bg("logs", "job", *bad)
        assert r["rc"] != 0 and r["err"].startswith("boxen run: "), bad
    r = bg.bg("logs", "nosuch")
    assert r["rc"] == 1 and "no process called nosuch" in r["err"]
    # restarting a finished one is fine; arguments survive as they are
    r = bg.bg("start", "job", "printf", "[%s]", "a b", "$HOME", "*")
    assert r["rc"] == 0
    assert wait_for(lambda: bg.bg("logs", "job")["out"] == "[a b][$HOME][*]")


def test_bg_names_and_usage(bg):
    for name in ("Bad", "-x", "a/b", "../x", "a" * 33, "ok\n../../x", "", "a b", ".x"):
        for cmd in (["start", name, "true"], ["stop", name], ["logs", name]):
            r = bg.bg(*cmd)
            assert r["rc"] == 1 and "lowercase letters, digits, - and _, up to 32" in r["err"], (cmd, r)
    assert not bg.dir.exists() or os.listdir(bg.dir) == []
    r = bg.bg("start", "a" * 32, "true")
    assert r["rc"] == 0
    for args, msg in ((["start", "web"], "usage: boxen run -d NAME cmd..."), (["stop"], "usage: boxen run --stop"),
                      (["stop", "a", "b"], "usage: boxen run --stop"), (["logs"], "usage: boxen run --logs"),
                      (["frob"], "usage: boxen-bg"), ([], "usage: boxen-bg")):
        r = bg.bg(*args)
        assert r["rc"] == 1 and msg in r["err"], args


def test_bg_ps_empty(bg):
    assert bg.bg("ps")["out"] == "no background processes (start one: boxen run -d NAME cmd...)\n"


def test_image_installs_boxen_exec_and_bg():
    text = open(os.path.join(REPO, "image", "Dockerfile")).read()
    assert "COPY boxen-exec boxen-bg /usr/local/bin/" in text
    assert "RUN chmod 0755 /usr/local/bin/boxen-exec /usr/local/bin/boxen-bg" in text
    # caches on the cache volume, toolchains in the image
    assert "XDG_CACHE_HOME=/root/.cache" in text and "CARGO_HOME=/opt/cargo" in text
    assert "RUSTUP_HOME=/opt/rustup" in text and "PATH=/opt/cargo/bin:" in text
    for f in (BG, EXEC):
        assert open(f).readline() == "#!/bin/sh\n"


# --------------------------------------------------------------- boxen-exec

RID = "0123456789abcdef01234567"


@pytest.fixture
def bx(tmp_path):
    """Run image/boxen-exec under /bin/sh (dash, as in the container)."""
    d = tmp_path / "exec"
    env = dict(os.environ, BOXEN_EXEC_DIR=str(d))

    def run(*args, **kw):
        return subprocess.run(["/bin/sh", EXEC] + list(args), env=env, capture_output=True, text=True,
                              timeout=30, **kw)

    def start(*args):
        return subprocess.Popen(["/bin/sh", EXEC] + list(args), env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    run.start = start
    run.dir = d
    yield run
    for f in d.glob("*") if d.exists() else []:
        try:
            os.killpg(int(f.read_text()), 9)
        except (OSError, ValueError):
            pass


def test_exec_runs_the_command(bx):
    r = bx(RID, "tr", "a-z", "A-Z", input="hello\nthere\n")             # stdin reaches it
    assert r.returncode == 0 and r.stdout == "HELLO\nTHERE\n" and r.stderr == ""
    r = bx(RID, "sh", "-c", "echo out; echo err >&2; exit 9")
    assert (r.returncode, r.stdout, r.stderr) == (9, "out\n", "err\n")
    r = bx(RID, "printf", "[%s]", "a b", "$HOME", "*", "")
    assert r.stdout == "[a b][$HOME][*][]"
    assert os.listdir(bx.dir) == []                                       # the id's file goes with it
    # in a session (and process group) of its own, recorded under its id
    r = bx(RID, "sh", "-c", 'cat "$0"; ps -o pid=,pgid=,sid= -p $$', str(bx.dir / RID))
    pid, pgid, sid = r.stdout.split("\n", 1)[1].split()
    assert r.stdout.split("\n")[0] == pid == pgid == sid


def test_exec_kill_ends_the_group(bx):
    p = bx.start(RID, "sh", "-c", "sleep 60 & sleep 60; wait")
    try:
        assert wait_for(lambda: (bx.dir / RID).exists() and (bx.dir / RID).read_text().strip())
        pgid = int((bx.dir / RID).read_text())
        assert wait_for(lambda: len(group_members(pgid)) >= 3)
        t = time.monotonic()
        r = bx("--kill", RID)
        assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
        assert group_members(pgid) == []
        p.wait(10)
        assert p.returncode == 143                                         # the command's TERM
        assert time.monotonic() - t < 5
        assert not (bx.dir / RID).exists()
    finally:
        if p.poll() is None:
            p.kill()


def test_exec_kill_escalates(bx):
    """A command that ignores TERM gets KILL after 5 seconds."""
    p = bx.start(RID, "sh", "-c", "trap '' TERM; sleep 60 & wait; wait")
    try:
        assert wait_for(lambda: (bx.dir / RID).exists() and (bx.dir / RID).read_text().strip())
        pgid = int((bx.dir / RID).read_text())
        assert wait_for(lambda: len(group_members(pgid)) >= 2)
        t = time.monotonic()
        assert bx("--kill", RID).returncode == 0
        assert 4 < time.monotonic() - t < 15
        assert group_members(pgid) == []
        p.wait(10)
    finally:
        if p.poll() is None:
            p.kill()


def test_exec_ids_and_usage(bx):
    for bad in ("short", "a/b/../../etc", "x" * 65, "0123456789\n../../x", "abc-defgh", "", "../passwd0"):
        r = bx(bad, "true")
        assert r.returncode == 125 and r.stderr == "boxen run: bad run id\n", bad
        r = bx("--kill", bad)
        assert r.returncode == 125 and r.stderr == "boxen run: bad run id\n", bad
    assert not bx.dir.exists() or os.listdir(bx.dir) == []
    for args in ([RID], [], ["--kill"], ["--kill", RID, "x"]):
        r = bx(*args)
        assert r.returncode == 125 and "usage: boxen-exec" in r.stderr, args
    # nothing (or garbage) recorded under an id: --kill does nothing, quietly
    assert bx("--kill", RID).returncode == 0
    bx.dir.mkdir(exist_ok=True)
    for junk in ("", "abc", "-1", "1 2", "12\n-1"):
        (bx.dir / RID).write_text(junk)
        r = bx("--kill", RID)
        assert r.returncode == 0 and r.stderr == "", junk
    # a command that can't be run: its exit code
    r = bx(RID, "/nonexistent/cmd")
    assert r.returncode == 127


# ----------------------------------------------------------- boxen-services

SERVICES = os.path.join(REPO, "bin", "boxen-services")


def services(doc, project="alpha"):
    r = subprocess.run([sys.executable, SERVICES, project], input=json.dumps(doc), capture_output=True, text=True)
    return r.returncode, (json.loads(r.stdout) if r.returncode == 0 else None), r.stderr


def svc(**kw):
    s = {"image": "postgres:16", "networks": {"default": None}}
    s.update(kw)
    return s


def test_services_ok():
    rc, out, err = services({"name": "w", "services": {
        "postgres": svc(environment=["A=1", "B=$x"], command=["postgres", "-c", "x=${Y}"],
                        volumes=[{"type": "volume", "source": "pgdata", "target": "/data", "read_only": True,
                                  "volume": {"nocopy": True}}],
                        depends_on={"cache": {"condition": "service_started", "required": True}},
                        healthcheck={"test": ["CMD", "true"], "interval": "5s"}),
        "cache": svc(image="redis:7", depends_on=["postgres"], tmpfs="/tmp")}})
    assert rc == 0, err
    pg = out["services"]["postgres"]
    assert pg["container_name"] == "boxen.alpha.postgres"
    assert pg["security_opt"] == ["no-new-privileges:true"] and pg["pids_limit"] == 2048
    assert pg["restart"] == "unless-stopped"
    assert pg["environment"] == ["A=1", "B=$$x"] and pg["command"] == ["postgres", "-c", "x=$${Y}"]
    assert pg["volumes"] == [{"type": "volume", "source": "boxen.alpha.pgdata", "target": "/data",
                              "read_only": True, "volume": {"nocopy": True}}]
    assert "networks" not in pg
    assert out["volumes"] == {"boxen.alpha.pgdata": {"name": "boxen.alpha.pgdata"}}
    assert out["services"]["cache"]["tmpfs"] == ["/tmp"]
    assert out["services"]["cache"]["depends_on"] == {"postgres": {"condition": "service_started", "required": True}}
    # a $$ (compose's way to write one literal $, uninterpolated here) stays one $
    rc, out, err = services({"services": {"db": svc(environment={"A": "a$$b", "B": "$$$$", "C": "$x$$y",
                                                                 "D": "$$$z"},
                                                    command="sh -c 'echo $$HOME'",
                                                    labels={"app": "x$$", "boxen-ish": "com.docker.compose.y"})}})
    assert rc == 0, err
    db = out["services"]["db"]
    assert db["environment"] == {"A": "a$$b", "B": "$$$$", "C": "$$x$$y", "D": "$$$$z"}
    assert db["command"] == "sh -c 'echo $$HOME'"
    assert db["labels"] == {"app": "x$$", "boxen-ish": "com.docker.compose.y"}
    # nothing at all
    assert services({"services": None})[:2] == (0, {"services": {}, "volumes": {}})


@pytest.mark.parametrize("name,service,msg", [
    ("env", svc(), "'env' is this project's own build container"),
    ("claude", svc(), "'claude' is this project's own build container"),
    ("boxen-x", svc(), "names starting with 'boxen' are reserved"),
    ("Bad", svc(), "name must match"),
    ("db", svc(volumes=[{"type": "volume", "source": "home", "target": "/x"}]),
     "'home' is reserved for the build container"),
    ("db", svc(volumes=[{"type": "volume", "source": "cache", "target": "/x"}]),
     "'cache' is reserved for the build container"),
    ("db", svc(labels={"com.docker.compose.project": "boxen-other"}), "labels starting with com.docker.compose."),
    ("db", svc(labels=["boxen.project=other"]), "labels starting with com.docker.compose. or boxen. are reserved"),
    ("db", svc(labels={"boxen.x": None}), "are reserved"),
    ("db", svc(volumes=[{"type": "bind", "source": "/etc", "target": "/x"}]), "/etc is a host path"),
    ("db", svc(volumes=[{"type": "bind", "source": "/run/user/1000/docker.sock", "target": "/s"}]),
     "the Docker socket can't be mounted"),
    ("db", svc(volumes=[{"type": "volume", "source": "x", "target": "/x", "volume": {"driver_opts": {}}}]),
     "volume options other than nocopy"),
    ("db", svc(volumes=[{"type": "volume", "source": "x", "target": "rel"}]), "must be absolute"),
    ("db", svc(volumes=[{"type": "volume", "source": "x", "target": "/x", "bind": {}}]), "option bind isn't allowed"),
    ("db", svc(ports=[{"target": 80}]), "ports isn't allowed in a sidecar"),
    ("db", svc(privileged=True), "privileged isn't allowed"),
    ("db", svc(network_mode="host"), "network_mode isn't allowed"),
    ("db", svc(networks={"other": None}), "networks isn't allowed"),
    ("db", svc(cap_add=["SYS_ADMIN"]), "cap_add isn't allowed"),
    ("db", svc(build={"context": "."}), "build isn't allowed"),
    ("db", svc(env_file=["/etc/shadow"]), "env_file isn't allowed"),
    ("db", svc(devices=["/dev/kvm"]), "devices isn't allowed"),
    ("db", svc(pid="host"), "pid isn't allowed"),
    ("db", svc(frobnicate=1), "frobnicate isn't an allowed key"),
    ("db", svc(image="boxen-env:latest"), "image names starting with 'boxen' are boxen's own"),
    ("db", svc(image=None), "image is required"),
    ("db", svc(environment=["NOVALUE"]), "has no value"),
    ("db", svc(environment={"X": None}), "has no value"),
    ("db", svc(depends_on=["nosuch"]), "isn't another sidecar"),
    ("db", svc(restart="sometimes"), "restart must be"),
    ("db", svc(working_dir="rel"), "working_dir must be absolute"),
])
def test_services_refused(name, service, msg):
    rc, out, err = services({"services": {name: service}})
    assert rc == 2 and out is None
    assert msg in err, err
    assert err.startswith("services.yaml: ")


def test_services_document_level():
    assert services([])[0] == 2
    assert "include isn't allowed" in services({"include": ["x.yaml"], "services": {}})[2]
    rc, _, err = services({"services": {"s%d" % i: svc() for i in range(9)}})
    assert rc == 2 and "at most 8 services" in err
    r = subprocess.run([sys.executable, SERVICES, "alpha"], input="not json", capture_output=True, text=True)
    assert r.returncode == 2 and "isn't JSON" in r.stderr
    r = subprocess.run([sys.executable, SERVICES], input="{}", capture_output=True, text=True)
    assert r.returncode == 2 and "usage" in r.stderr
    # the user's top-level volumes (driver_opts could mount a host path) are replaced
    rc, out, _ = services({"services": {"db": svc(volumes=[{"type": "volume", "source": "d", "target": "/d"}])},
                           "volumes": {"d": {"driver_opts": {"type": "none", "device": "/etc", "o": "bind"}}}})
    assert rc == 0 and out["volumes"] == {"boxen.alpha.d": {"name": "boxen.alpha.d"}}


# ------------------------------------------------------------ static checks

SHELL = ["bin/boxen", "lib/github.sh", "lib/doctor.sh", "lib/migrate.sh", "tools/mac-to-boxen", "bin/boxen-gh",
         "image/boxen-bg", "image/boxen-exec", "install.sh"]


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck isn't installed")
def test_shellcheck():
    r = subprocess.run(["shellcheck", "-x", "-S", "warning"] + SHELL, cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_syntax():
    for f in SHELL:
        path = os.path.join(REPO, f)
        first = open(path).readline()
        shell = "sh" if first.startswith("#!/bin/sh") else "bash"
        r = subprocess.run([shell, "-n", path], capture_output=True, text=True)
        assert r.returncode == 0, (f, r.stderr)
        if shell == "sh":
            r = subprocess.run(["bash", "--posix", "-n", path], capture_output=True, text=True)
            assert r.returncode == 0, (f, r.stderr)
    for f in ("bin/boxen-services", "bin/boxen-gitscan"):
        r = subprocess.run([sys.executable, "-m", "py_compile", os.path.join(REPO, f)], capture_output=True,
                           text=True, env=dict(os.environ, PYTHONPYCACHEPREFIX=os.devnull + "x"))
        compile(open(os.path.join(REPO, f)).read(), f, "exec")


def test_scripts_are_executable():
    for f in ("bin/boxen", "bin/boxen-gh", "bin/boxen-services", "bin/boxen-gitscan", "bin/boxen-carry",
              "image/boxen-bg", "image/boxen-exec", "install.sh"):
        assert os.access(os.path.join(REPO, f), os.X_OK), f
