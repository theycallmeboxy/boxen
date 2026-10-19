"""Tests for bin/boxen's `ls` (--json, the state cache), its project locks,
`ui`, ui/install-venv.sh and boxen-agentd's probe handling, with fake
docker, tmux and python3 commands. Run: python3 -m pytest tests"""
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOXEN = os.path.join(REPO, "bin", "boxen")

FAKE_DOCKER = r'''#!PYTHON
import json, os, sys, time
d = os.environ["FAKE_DIR"]
a = sys.argv[1:]
open(d + "/docker.log", "a").write(json.dumps(a) + "\n")
if os.path.exists(d + "/down"):
    sys.stderr.write("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?\n")
    sys.exit(1)
state = json.load(open(d + "/docker.json"))
if a[:2] == ["inspect", "-f"]:
    f, names = a[2], a[3:]
    rc = 0
    for n in names:
        c = state.get(n)
        if c is None:
            sys.stderr.write("Error: No such object: %s\n" % n); rc = 1; continue
        if f.startswith("C "):
            print("C /%s %s %d %s %s" % (n, c["status"], c["restarts"], c["started"], c["id"]))
        elif f == "{{.State.StartedAt}}":
            print(c["started"])
        else:
            sys.stderr.write("fake docker: unknown format %s\n" % f); sys.exit(1)
    sys.exit(rc)
if a[:1] == ["logs"]:
    sys.stdout.write((state.get(a[-1]) or {}).get("logs", ""))
    sys.exit(0)
if a[:1] == ["compose"]:
    time.sleep(float(os.environ.get("FAKE_COMPOSE_SLEEP", "0")))
    sys.exit(0)
if a[:1] in (["build"], ["tag"], ["builder"]) or a[:2] == ["image", "prune"]:
    sys.exit(0)
sys.stderr.write("fake docker: unhandled %r\n" % a); sys.exit(1)
'''

STARTED = "2026-10-19T08:00:00.123456789Z"


def write_exe(path, content):
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, 0o755)


@pytest.fixture
def vm(tmp_path):
    """A BOXEN_HOME with projects alpha (running, two sidecars, a GitHub
    remote), beta (never started) and the supervisor; fake docker."""
    home = tmp_path / "home"
    src = tmp_path / "src"
    fake = tmp_path / "fake"
    bindir = tmp_path / "bin"
    for d in (home / "projects", src, fake, bindir):
        d.mkdir(parents=True)
    for n in ("alpha", "beta", "supervisor"):
        (home / "projects" / n).mkdir()
        (home / "projects" / n / "compose.yaml").write_text("name: boxen-%s\n" % n)
    # cache: no container_name, no image (the empty fields that used to shift columns)
    (home / "projects" / "alpha" / "compose.services.yaml").write_text(json.dumps(
        {"services": {"db": {"image": "postgres:16", "container_name": "boxen.alpha.db"},
                      "cache": {}}}))
    (src / "alpha").mkdir()
    subprocess.run(["git", "init", "-q", str(src / "alpha")], check=True)
    subprocess.run(["git", "-C", str(src / "alpha"), "remote", "add", "origin",
                    "https://github.com/theycallmeboxy/alpha.git"], check=True)
    state = {
        "boxen-alpha": {"status": "running", "restarts": 2, "started": STARTED, "id": "a" * 64,
                        "logs": "Remote Control starts...\nConnected\n"},
        "boxen-supervisor": {"status": "exited", "restarts": 0, "started": STARTED, "id": "5" * 64},
    }
    (fake / "docker.json").write_text(json.dumps(state))
    write_exe(str(bindir / "docker"), FAKE_DOCKER.replace("#!PYTHON", "#!" + sys.executable))
    env = dict(os.environ, BOXEN_HOME=str(home), BOXEN_SRC=str(src), FAKE_DIR=str(fake),
               PATH=str(bindir) + os.pathsep + os.environ["PATH"])
    env.pop("BOXEN_AGENT", None)
    env.pop("TMUX", None)
    return {"env": env, "home": home, "fake": fake, "bin": bindir, "tmp": tmp_path, "state": state}


def docker_calls(vm):
    p = vm["fake"] / "docker.log"
    return [json.loads(line) for line in open(p)] if p.exists() else []


def ls_json(vm):
    out = subprocess.run([BOXEN, "ls", "--json"], env=vm["env"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_ls_json(vm):
    doc = ls_json(vm)
    assert doc["version"] == 1 and doc["docker"] is True
    assert [p["name"] for p in doc["projects"]] == ["alpha", "beta"]
    alpha, beta = doc["projects"]
    assert alpha["container"] == {"status": "running", "restarts": 2, "started_at": STARTED, "id": "a" * 64}
    assert alpha["server"] == "connected"
    assert alpha["github"] == "run boxen github"           # remote, but no GitHub App yet
    assert alpha["repo"] == "theycallmeboxy/alpha"
    # the configured sidecars only (their state is Docker's); empty fields stay in place
    assert alpha["sidecars"] == [
        {"service": "db", "container": "boxen.alpha.db", "image": "postgres:16"},
        {"service": "cache", "container": "boxen.alpha.cache", "image": ""}]
    assert beta == {"name": "beta", "container": None, "server": "-", "github": "-", "repo": None,
                    "sidecars": []}
    sup = doc["supervisor"]
    assert sup["name"] == "supervisor" and sup["container"]["status"] == "exited" and sup["server"] == "-"
    calls = docker_calls(vm)
    # one inspect for every container; the running server's log once
    assert [c[0] for c in calls] == ["inspect", "logs"]
    assert calls[0][3:] == ["boxen-alpha", "boxen-beta", "boxen-supervisor"]
    assert calls[1] == ["logs", "--since", STARTED, "--tail", "300", "boxen-alpha"]


def test_ls_json_no_projects_and_docker_down(vm):
    shutil.rmtree(vm["home"] / "projects")
    (vm["home"] / "projects").mkdir()
    assert ls_json(vm) == {"version": 1, "docker": True, "projects": [], "supervisor": None}
    (vm["home"] / "projects" / "solo").mkdir()
    (vm["home"] / "projects" / "solo" / "compose.yaml").write_text("x")
    (vm["fake"] / "down").write_text("")
    doc = ls_json(vm)
    assert doc["docker"] is False
    assert doc["projects"][0]["name"] == "solo" and doc["projects"][0]["container"] is None
    # docker not installed at all: a PATH with everything in /usr/bin but docker
    os.unlink(vm["bin"] / "docker")
    nodocker = vm["tmp"] / "nodocker"
    nodocker.mkdir()
    for d in ("/usr/bin", "/bin"):
        for n in os.listdir(d):
            if n != "docker" and not (nodocker / n).exists():
                os.symlink(os.path.join(d, n), nodocker / n)
    env = dict(vm["env"], PATH="%s:%s" % (vm["bin"], nodocker))
    assert shutil.which("docker", path=env["PATH"]) is None
    out = subprocess.run([BOXEN, "ls", "--json"], env=env, capture_output=True, text=True)
    assert json.loads(out.stdout)["docker"] is False


def test_server_state_cache(vm):
    ls_json(vm)
    cache = vm["home"] / "projects" / "alpha" / ".state-cache"
    assert cache.read_text().split(" ", 3)[3] == "connected\n"
    n_logs = lambda: len([c for c in docker_calls(vm) if c[0] == "logs"])  # noqa: E731
    assert n_logs() == 1
    # connected, same container, under 60 s: the cache answers
    assert ls_json(vm)["projects"][0]["server"] == "connected" and n_logs() == 1
    out = subprocess.run([BOXEN, "ls"], env=vm["env"], capture_output=True, text=True)
    assert "connected" in out.stdout and n_logs() == 1      # plain ls too
    # older than 60 s: read again
    f = cache.read_text().split(" ")
    f[2] = str(int(f[2]) - 61)
    cache.write_text(" ".join(f))
    ls_json(vm)
    assert n_logs() == 2
    # the container restarted (new StartedAt): read again
    vm["state"]["boxen-alpha"]["started"] = "2026-10-19T09:00:00Z"
    (vm["fake"] / "docker.json").write_text(json.dumps(vm["state"]))
    ls_json(vm)
    assert n_logs() == 3
    # not settled (waiting at a question): read every time
    vm["state"]["boxen-alpha"]["logs"] = "Remote Control starts Claude Code sessions in this folder\n"
    (vm["fake"] / "docker.json").write_text(json.dumps(vm["state"]))
    vm["state"]["boxen-alpha"]["started"] = "2026-10-19T10:00:00Z"
    (vm["fake"] / "docker.json").write_text(json.dumps(vm["state"]))
    assert ls_json(vm)["projects"][0]["server"] == "waiting for an answer: run boxen up alpha"
    ls_json(vm)
    assert n_logs() == 5
    # a planted link isn't read through
    cache.unlink()
    os.symlink("/etc/hostname", cache)
    assert ls_json(vm)["projects"][0]["server"].startswith("waiting")


def test_ls_plain_unchanged(vm):
    out = subprocess.run([BOXEN, "ls"], env=vm["env"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert lines[0] == "%-24s %-20s %-42s %s" % ("PROJECT", "CONTAINER", "SERVER", "GITHUB")
    assert lines[1] == "%-24s %-20s %-42s %s" % ("alpha", "running (2 restarts)", "connected", "run boxen github")
    assert lines[2] == "%-24s %-20s %-42s %s" % ("beta", "not created", "-", "-")
    assert lines[3] == "%-24s %-20s %-42s %s" % ("supervisor", "exited", "-", "-")
    out = subprocess.run([BOXEN, "ls", "--yaml"], env=vm["env"], capture_output=True, text=True)
    assert out.returncode == 1 and "usage: boxen ls [--json]" in out.stderr


def test_help_aligned():
    """Every command's description starts in the same column."""
    lines = [x for x in subprocess.run([BOXEN, "help"], capture_output=True, text=True).stdout.splitlines()[2:] if x]
    starts = {len(x) - len(x[2:].split("  ", 1)[1].lstrip()) for x in lines}
    assert starts == {50}, lines


# --------------------------------------------------------------- locks

def test_project_lock_held_elsewhere(vm):
    """While another boxen holds alpha's lock, a mutating command exits 2."""
    lock = vm["home"] / "projects" / "alpha" / ".lock"
    with open(lock, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for args in (["up", "alpha"], ["down", "alpha"], ["restart", "alpha"], ["update", "alpha"],
                     ["rm", "alpha", "--yes"]):
            out = subprocess.run([BOXEN] + args, env=vm["env"], capture_output=True, text=True)
            assert out.returncode == 2, (args, out.stderr)
            assert out.stderr == "boxen: alpha is busy (another boxen command is running for it)\n"
        # reading isn't locked
        assert subprocess.run([BOXEN, "ls", "--json"], env=vm["env"], capture_output=True).returncode == 0
    with open(vm["home"] / ".global.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        out = subprocess.run([BOXEN, "update"], env=vm["env"], capture_output=True, text=True)
        assert out.returncode == 2 and "busy (another update or self-update is running)" in out.stderr


def test_two_concurrent_downs(vm):
    """Two `boxen down alpha` at once: the second exits 2 with the message."""
    env = dict(vm["env"], FAKE_COMPOSE_SLEEP="2")
    first = subprocess.Popen([BOXEN, "down", "alpha"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
    lock = vm["home"] / "projects" / "alpha" / ".lock"
    for _ in range(100):                     # wait until the first holds the lock
        try:
            with open(lock, "a") as f:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f, fcntl.LOCK_UN)
            time.sleep(0.05)
        except (BlockingIOError, FileNotFoundError):
            if lock.exists():
                break
            time.sleep(0.05)
    second = subprocess.run([BOXEN, "down", "alpha"], env=env, capture_output=True, text=True)
    out, err = first.communicate(timeout=30)
    assert second.returncode == 2
    assert second.stderr == "boxen: alpha is busy (another boxen command is running for it)\n"
    assert first.returncode == 0, err
    assert "alpha: stopped" in out


def test_reserved_names(vm):
    """New projects can't take boxen ui's names; one that has one already
    (made before they were reserved) keeps working."""
    for name in ("global", "dashboard", "ui"):
        out = subprocess.run([BOXEN, "new", name], env=vm["env"], capture_output=True, text=True)
        assert out.returncode == 1 and "'%s' is reserved" % name in out.stderr
    (vm["home"] / "projects" / "ui").mkdir()
    (vm["home"] / "projects" / "ui" / "compose.yaml").write_text("x")
    out = subprocess.run([BOXEN, "show", "ui"], env=vm["env"], capture_output=True, text=True)
    assert out.returncode == 0 and "ui: sidecars" in out.stdout, out.stderr


def hold_lock(path):
    f = open(path, "a")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


def test_all_skips_busy_projects(vm):
    with hold_lock(vm["home"] / "projects" / "alpha" / ".lock"):
        out = subprocess.run([BOXEN, "down", "all"], env=vm["env"], capture_output=True, text=True)
    assert out.returncode == 2
    assert "beta: stopped" in out.stdout and "supervisor: stopped" in out.stdout
    assert "boxen: alpha is busy" in out.stderr
    assert "skipped, busy (another boxen command was running for them): alpha" in out.stderr
    assert "failed for" not in out.stderr


def test_update_tells_busy_from_failed(vm):
    with hold_lock(vm["home"] / "projects" / "alpha" / ".lock"):
        out = subprocess.run([BOXEN, "update"], env=vm["env"], capture_output=True, text=True, timeout=120)
    assert out.returncode == 1
    assert "update: skipped, busy (another boxen command was running for them): alpha" in out.stderr
    assert "these projects failed: beta supervisor" in out.stderr       # the fake never runs them


def lock_holders(path):
    """PIDs with path open (from /proc/*/fd)."""
    path = os.path.realpath(path)
    pids = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            for fd in os.listdir("/proc/%s/fd" % pid):
                try:
                    if os.readlink("/proc/%s/fd/%s" % (pid, fd)) == path:
                        pids.append(int(pid))
                except OSError:
                    pass
        except OSError:
            pass
    return pids


def test_self_update_leaves_no_lock_behind(vm, tmp_path):
    """The reviewer's case: self-update runs the new install.sh, which can
    start a daemon with nohup (the agent's cron path). The daemon must not
    inherit the global lock: afterwards `update` isn't busy and nothing
    holds .global.lock."""
    work = tmp_path / "work"
    shutil.copytree(REPO, work, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
    g = ["git", "-C", str(work), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["commit", "-qm", "wip", "--allow-empty"], check=True)
    branch = subprocess.run(g + ["rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
    up = tmp_path / "up.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(up)], check=True)
    subprocess.run(g + ["remote", "remove", "origin"], capture_output=True)
    subprocess.run(g + ["remote", "add", "origin", str(up)], check=True)
    subprocess.run(g + ["fetch", "-q", "origin"], check=True)
    subprocess.run(g + ["branch", "-q", "--set-upstream-to=origin/" + branch], check=True)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(up), str(other)], check=True)
    pidfile = vm["fake"] / "daemon.pid"
    (other / "install.sh").write_text("#!/bin/bash\n# like install_agent's cron path\n"
                                      "nohup sleep 60 >/dev/null 2>&1 &\necho $! > %s\ndisown\n" % pidfile)
    go = ["git", "-C", str(other), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(go + ["commit", "-qam", "new installer"], check=True)
    subprocess.run(go + ["push", "-q", "origin", branch], check=True)
    shutil.rmtree(vm["home"] / "projects")
    (vm["home"] / "projects").mkdir()
    out = subprocess.run([str(work / "bin" / "boxen"), "self-update"], env=vm["env"], capture_output=True,
                         text=True, timeout=120)
    try:
        assert out.returncode == 0, out.stderr
        assert "self-update done" in out.stdout
        daemon = int(pidfile.read_text())
        assert os.path.exists("/proc/%d" % daemon)                    # still running
        lock = vm["home"] / ".global.lock"
        assert lock_holders(lock) == []
        with hold_lock(lock):
            pass                                                      # free
        out = subprocess.run([str(work / "bin" / "boxen"), "update", "nosuch"], env=vm["env"],
                             capture_output=True, text=True)
        assert "busy" not in out.stderr
    finally:
        try:
            os.kill(int(pidfile.read_text()), 15)
        except (OSError, ValueError):
            pass


def test_agentd_closes_inherited_descriptors(tmp_path):
    """Even started holding a lock (a leak somewhere), the agent lets it go."""
    home = tmp_path / "h"
    (home / "projects").mkdir(parents=True)
    lock = tmp_path / "g.lock"
    f = hold_lock(lock)
    proc = subprocess.Popen([sys.executable, os.path.join(REPO, "bin", "boxen-agentd")],
                            env=dict(os.environ, BOXEN_HOME=str(home)), pass_fds=(f.fileno(),),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_path(home / "agent" / "agent.sock")
        f.close()
        assert proc.pid not in lock_holders(lock)
        with hold_lock(lock):
            pass
    finally:
        proc.terminate()
        proc.wait(5)


# --------------------------------------------------------------- boxen ui

@pytest.fixture
def ui(vm):
    """Fake tmux (the UI tests' one) and a fake versioned ui venv behind a link."""
    write_exe(str(vm["bin"] / "tmux"), "#!/bin/sh\nexec %s %s \"$@\"\n" % (
        sys.executable, os.path.join(REPO, "ui", "tests", "fakes", "fake_tmux.py")))
    data = vm["tmp"] / "data"
    venv = data / "ui-venv-0123456789ab"
    (venv / "bin").mkdir(parents=True)
    write_exe(str(venv / "bin" / "python"), "#!/bin/sh\n[ \"$1\" = -c ] && exit ${FAKE_PY_OLD:-0}\nexit 0\n")
    os.symlink(venv.name, data / "ui-venv")
    vm["env"]["BOXEN_UI_VENV"] = str(data / "ui-venv")
    vm["venv"] = venv
    return vm


def tmux_calls(vm):
    p = vm["fake"] / "tmux.jsonl"
    return [json.loads(line)["argv"] for line in open(p)] if p.exists() else []


def with_tty(cmd, env):
    """Run cmd (argv) with a terminal, through script(1)."""
    return subprocess.run(["script", "-qec", shlex.join(cmd), "/dev/null"], env=env,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)


def dash_argv(vm):
    # the venv's real folder, not the link; -P: nothing from the working directory
    return ["env", "PYTHONPATH=%s/ui" % REPO, "sh", "-c",
            '"$0" -P -m boxen_ui || { printf "\\nboxen ui exited (%s); press Enter " "$?"; read -r _; }',
            str(vm["venv"] / "bin" / "python")]


def test_ui_refuses_without_tty_and_in_agent_mode(ui):
    out = subprocess.run([BOXEN, "ui"], env=ui["env"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert out.returncode == 1 and "needs a terminal" in out.stderr
    env = dict(ui["env"], BOXEN_AGENT="1")
    out = with_tty([BOXEN, "ui"], env)
    assert out.returncode == 2 and "ui needs a terminal" in out.stdout
    assert [c for c in tmux_calls(ui) if c != ["-V"]] == []


def test_ui_starts_tmux_session(ui):
    out = with_tty([BOXEN, "ui"], ui["env"])
    assert out.returncode == 0, out.stdout
    calls = tmux_calls(ui)
    assert ["-L", "boxen", "has-session", "-t", "boxen"] in calls
    assert calls[-1] == ["-L", "boxen", "-f", os.path.join(REPO, "ui", "tmux.conf"), "new-session", "-A",
                         "-c", os.environ["HOME"], "-s", "boxen", "-n", "dashboard"] + dash_argv(ui)


def test_ui_versions(ui):
    (ui["fake"] / "version").write_text("tmux 3.2a")
    out = with_tty([BOXEN, "ui"], ui["env"])
    assert out.returncode == 1 and "needs tmux 3.3 or newer (this is tmux 3.2a)" in out.stdout
    (ui["fake"] / "version").write_text("tmux next-3.6")
    assert with_tty([BOXEN, "ui"], ui["env"]).returncode == 0
    out = with_tty([BOXEN, "ui"], dict(ui["env"], FAKE_PY_OLD="1"))
    assert out.returncode == 1 and "needs Python 3.11 or newer" in out.stdout


def test_ui_reattaches_and_restores_dashboard(ui):
    (ui["fake"] / "has-session").write_text("")
    (ui["fake"] / "windows.json").write_text(json.dumps([{"id": "@1", "name": "caulker/shell"}]))
    out = with_tty([BOXEN, "ui"], ui["env"])
    assert out.returncode == 0, out.stdout
    calls = tmux_calls(ui)
    assert ["-L", "boxen", "source-file", os.path.join(REPO, "ui", "tmux.conf")] in calls
    assert ["-L", "boxen", "new-window", "-d", "-c", os.environ["HOME"], "-t", "boxen:", "-n",
            "dashboard"] + dash_argv(ui) in calls
    assert calls[-1][4:7] == ["new-session", "-A", "-c"]
    (ui["fake"] / "tmux.jsonl").unlink()
    (ui["fake"] / "windows.json").write_text(json.dumps([{"id": "@0", "name": "dashboard"}]))
    with_tty([BOXEN, "ui"], ui["env"])
    assert not any("new-window" in c for c in tmux_calls(ui))


def test_ui_inside_session_selects_dashboard(ui):
    env = dict(ui["env"], TMUX="/tmp/tmux-1000/boxen,123,0")
    (ui["fake"] / "windows.json").write_text(json.dumps([{"id": "@0", "name": "dashboard"}]))
    out = with_tty([BOXEN, "ui"], env)
    assert out.returncode == 0, out.stdout
    assert [c for c in tmux_calls(ui) if c != ["-V"]] == [["-L", "boxen", "select-window", "-t", "boxen:=dashboard"]]
    (ui["fake"] / "tmux.jsonl").unlink()
    (ui["fake"] / "windows.json").write_text("[]")
    with_tty([BOXEN, "ui"], env)
    assert tmux_calls(ui)[-1] == ["-L", "boxen", "new-window", "-c", os.environ["HOME"], "-t", "boxen:", "-n",
                                  "dashboard"] + dash_argv(ui)
    (ui["fake"] / "tmux.jsonl").unlink()
    env = dict(ui["env"], TMUX="/tmp/tmux-1000/default,123,0")
    with_tty([BOXEN, "ui"], env)
    assert tmux_calls(ui)[-1][4] == "new-session"


def test_ui_restart(ui):
    out = subprocess.run([BOXEN, "ui", "--restart"], env=ui["env"], capture_output=True, text=True)
    assert out.returncode == 1 and "isn't running" in out.stderr
    (ui["fake"] / "has-session").write_text("")
    out = subprocess.run([BOXEN, "ui", "--restart"], env=ui["env"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert tmux_calls(ui)[-1] == ["-L", "boxen", "respawn-window", "-k", "-c", os.environ["HOME"], "-t",
                                  "boxen:=dashboard"] + dash_argv(ui)


def test_ui_without_venv(ui):
    os.unlink(ui["env"]["BOXEN_UI_VENV"])
    out = with_tty([BOXEN, "ui"], ui["env"])
    assert out.returncode == 1 and "install.sh" in out.stdout


def test_dashboard_ignores_the_working_directory(tmp_path):
    """The reviewer's PoC: a planted `rich` in the working directory is
    imported by `python -m boxen_ui`, never with -P (how boxen ui runs it)."""
    poc = tmp_path / "cwd"
    (poc / "rich").mkdir(parents=True)
    (poc / "rich" / "__init__.py").write_text('raise SystemExit("PLANTED rich from cwd")\n')
    env = dict(os.environ, PYTHONPATH=os.path.join(REPO, "ui"))
    bad = subprocess.run([sys.executable, "-m", "boxen_ui", "--check"], cwd=poc, env=env,
                         capture_output=True, text=True)
    assert "PLANTED" in bad.stderr                      # the harness does what it says
    good = subprocess.run([sys.executable, "-P", "-m", "boxen_ui", "--check"], cwd=poc, env=env,
                          capture_output=True, text=True)
    assert good.returncode == 0 and "PLANTED" not in good.stdout + good.stderr, good.stderr
    assert good.stdout.startswith("boxen ui ok")


# --------------------------------------------------------- install-venv.sh

FAKE_PY = r'''#!/bin/sh
# fake python3: `-m venv DIR` makes DIR/bin/python, a fake that logs pip runs
echo "python3 $*" >> "$FAKE_DIR/py.log"
[ "$1" = -m ] && [ "$2" = venv ] || exit 1
[ -z "$FAKE_NO_VENV" ] || exit 1
mkdir -p "$3/bin"
cat > "$3/bin/python" <<EOF
#!/bin/sh
echo "venv-python \$*" >> "$FAKE_DIR/py.log"
case "\$*" in *--check*) [ -z "\$FAKE_CHECK_FAIL" ] || exit 1; exit 0 ;; esac
[ -z "\$FAKE_PIP_FAIL" ] || exit 1
touch "$3/installed"
EOF
chmod +x "$3/bin/python"
'''


@pytest.fixture
def inst(tmp_path):
    repo = tmp_path / "repo"
    (repo / "ui").mkdir(parents=True)
    shutil.copy(os.path.join(REPO, "ui", "install-venv.sh"), repo / "ui")
    (repo / "ui" / "requirements.lock").write_text("textual==1.0 --hash=sha256:00\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    write_exe(str(bindir / "python3"), FAKE_PY)
    fake = tmp_path / "fake"
    fake.mkdir()
    data = tmp_path / "data" / "boxen"
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"], FAKE_DIR=str(fake),
               XDG_DATA_HOME=str(tmp_path / "data"))
    env.pop("BOXEN_UI_VENV", None)
    return {"repo": repo, "env": env, "data": data, "link": data / "ui-venv", "fake": fake,
            "run": lambda **kw: subprocess.run([str(repo / "ui" / "install-venv.sh")], env=dict(env, **kw),
                                               capture_output=True, text=True)}


def pylog(inst):
    p = inst["fake"] / "py.log"
    return p.read_text().splitlines() if p.exists() else []


def lock_hash(inst):
    return hashlib.sha256((inst["repo"] / "ui" / "requirements.lock").read_bytes()).hexdigest()


def test_install_venv_creates_and_skips(inst):
    r = inst["run"]()
    assert r.returncode == 0, r.stderr
    h = lock_hash(inst)
    v = inst["data"] / ("ui-venv-" + h[:12])
    assert os.readlink(inst["link"]) == v.name                 # a relative link to the version
    assert (v / "installed").exists() and (v / ".boxen-lock").read_text().strip() == h
    log = pylog(inst)
    assert log[0] == "python3 -m venv %s" % v
    assert log[1] == ("venv-python -m pip install --disable-pip-version-check -q --require-hashes "
                      "--only-binary=:all: --timeout 20 --retries 1 -r %s" % (inst["repo"] / "ui" / "requirements.lock"))
    assert log[2] == "venv-python -P -m boxen_ui --check"
    assert not any(".new" in x or ".tmp" in x for x in os.listdir(inst["data"]))
    r = inst["run"]()
    assert r.returncode == 0 and "up to date" in r.stdout and len(pylog(inst)) == 3
    # a new lock: a new folder, the link switched, the previous one kept
    (inst["repo"] / "ui" / "requirements.lock").write_text("textual==2.0 --hash=sha256:11\n")
    assert inst["run"]().returncode == 0
    v2 = inst["data"] / ("ui-venv-" + lock_hash(inst)[:12])
    assert os.readlink(inst["link"]) == v2.name and v.exists()
    # and the one before that goes at the next install
    (inst["repo"] / "ui" / "requirements.lock").write_text("textual==3.0 --hash=sha256:22\n")
    assert inst["run"]().returncode == 0
    assert not v.exists() and v2.exists()


def test_install_venv_failure_keeps_old(inst):
    assert inst["run"]().returncode == 0
    old = os.readlink(inst["link"])
    (inst["repo"] / "ui" / "requirements.lock").write_text("textual==9.9 --hash=sha256:99\n")
    r = inst["run"](FAKE_PIP_FAIL="1")
    assert r.returncode == 1 and "couldn't install" in r.stderr
    assert os.readlink(inst["link"]) == old
    assert not (inst["data"] / ("ui-venv-" + lock_hash(inst)[:12])).exists()   # no half-built folder
    r = inst["run"](FAKE_CHECK_FAIL="1")
    assert r.returncode == 1 and "doesn't start" in r.stderr and os.readlink(inst["link"]) == old
    assert inst["run"]().returncode == 0 and os.readlink(inst["link"]) != old


def test_install_venv_migrates_plain_folder(inst):
    inst["link"].mkdir(parents=True)
    (inst["link"] / "old-file").write_text("x")
    (inst["data"] / "ui-venv.sha256").write_text("x\n")
    assert inst["run"]().returncode == 0
    assert inst["link"].is_symlink() and (inst["data"] / "ui-venv-legacy" / "old-file").exists()
    assert not (inst["data"] / "ui-venv.sha256").exists()


def test_install_venv_no_python3_venv(inst):
    r = inst["run"](FAKE_NO_VENV="1")
    assert r.returncode == 1 and "python3-venv" in r.stderr
    assert not inst["link"].exists()


def test_lock_file_is_fully_hashed():
    """Every package in requirements.lock is pinned and has hashes."""
    text = open(os.path.join(REPO, "ui", "requirements.lock")).read()
    blocks, cur = [], None
    for line in text.splitlines():
        if line and not line.startswith((" ", "#")):
            cur = [line]
            blocks.append(cur)
        elif cur is not None and line.strip().startswith("--hash=sha256:"):
            cur.append(line)
    names = [b[0].split("==")[0] for b in blocks]
    assert "textual" in names and "rich" in names
    for b in blocks:
        assert "==" in b[0] and len(b) >= 2, b[0]


def test_install_sh_wiring():
    text = open(os.path.join(REPO, "install.sh")).read()
    pkgs = text.split("for pkg in ca-certificates", 1)[1].split(";", 1)[0].split()
    assert {"tmux", "python3-venv", "vim"} <= set(pkgs)
    assert '"$REPO_DIR/ui/install-venv.sh"' in text
    assert "tmux 3.3" in text
    # before self-update's early exit, so self-update refreshes it too
    assert text.index('"$REPO_DIR/ui/install-venv.sh"') < text.index('[[ -z "${BOXEN_SELF_UPDATE:-}" ]] || exit 0')


# ------------------------------------------------------------- agentd

def start_agentd(home):
    env = dict(os.environ, BOXEN_HOME=str(home))
    proc = subprocess.Popen([sys.executable, os.path.join(REPO, "bin", "boxen-agentd")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc


def wait_path(p, timeout=8):
    end = time.time() + timeout
    while time.time() < end and not p.exists():
        time.sleep(0.05)
    assert p.exists(), p


def poke(path, data=b""):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(str(path))
    if data:
        s.sendall(data)
    s.shutdown(socket.SHUT_WR)
    reply = s.makefile().read()
    s.close()
    return reply


def test_agentd_probes(tmp_path):
    """The supervisor socket: an empty connection is a probe, never logged.
    A project's socket: logged, at most once a minute."""
    home = tmp_path / "h"
    (home / "projects" / "alpha").mkdir(parents=True)
    proc = start_agentd(home)
    try:
        sock = home / "agent" / "agent.sock"
        psock = home / "projects" / "alpha" / "agent" / "agent.sock"
        wait_path(sock)
        wait_path(psock)
        assert poke(sock) == ""
        assert '"exit": 2' in poke(sock, b'{"argv": ["shell", "x"]}\n')
        for _ in range(3):
            assert poke(psock) == ""
        time.sleep(0.3)
        log = (home / "agent.log").read_text()
        assert "bad request" not in log and '"shell"' in log
        assert log.count("[alpha]") == 1 and "empty connection" in log
    finally:
        proc.terminate()
        proc.wait(5)
