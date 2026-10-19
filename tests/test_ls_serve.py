"""`boxen ls` (plain and --json, version 3), `boxen serve`, and
`boxen git-credential`."""
import json
import os
import shutil
import subprocess

import pytest

from conftest import BOXEN, REPO, wait_for


def ls_json(vm, **kw):
    r = vm.ok("ls", "--json", **kw)
    return json.loads(r.stdout)


# ---------------------------------------------------------------------- ls

def test_ls_json(vm):
    vm.container("boxen.alpha.env", "boxen-alpha", status="Up 2 hours")
    vm.container("boxen.alpha.db", "boxen-alpha", state="exited", status="Exited (1) 5 minutes ago")
    vm.container("other_web_1", "other")                         # not boxen's
    vm.container("boxen.gone.env", "boxen-gone")                 # no such project
    (vm.pdir("alpha") / "repos").write_text("# header\ntheycallmeboxy/alpha\nmyorg/x\n")
    doc = ls_json(vm)
    assert doc["version"] == 3 and doc["docker"] is True and doc["src"] == str(vm.src)
    assert doc["v1"] == ["old"]
    assert [p["name"] for p in doc["projects"]] == ["alpha", "beta"]
    alpha, beta = doc["projects"]
    assert alpha == {
        "name": "alpha", "path": str(vm.src / "alpha"),
        "container": {"state": "running", "status": "Up 2 hours"},
        "sidecars": [{"service": "db", "container": "boxen.alpha.db", "state": "exited",
                      "status": "Exited (1) 5 minutes ago"}],
        "sessions": 0, "serve": "off", "github": "run boxen github", "repo": "theycallmeboxy/alpha",
        "repos": [{"repo": "theycallmeboxy/alpha", "state": "-", "origin": True},
                  {"repo": "myorg/x", "state": "-", "origin": False}]}
    assert beta == {"name": "beta", "path": str(vm.src / "beta"), "container": None, "sidecars": [],
                    "sessions": 0, "serve": "off", "github": "-", "repo": None, "repos": []}
    # one docker call for every container
    assert [c[0] for c in vm.docker_calls()] == ["ps"]
    assert vm.docker_calls()[0][:4] == ["ps", "-a", "--filter", "label=com.docker.compose.project"]


def test_ls_plain(vm):
    (vm.pdir("beta") / "serve").write_text("")
    r = vm.ok("ls")
    lines = r.stdout.splitlines()
    fmt = "%-24s %-14s %-9s %-8s %s"
    assert lines[0] == fmt % ("PROJECT", "CONTAINER", "SESSIONS", "SERVE", "GITHUB")
    assert lines[1] == fmt % ("alpha", "running", "0", "off", "-")
    assert lines[2] == fmt % ("beta", "not created", "0", "stopped", "-")
    assert lines[3] == "boxen v1 projects, not moved yet (boxen migrate): old"
    assert len(lines) == 4


def test_ls_no_projects(vm):
    shutil.rmtree(vm.projects)
    assert ls_json(vm) == {"version": 3, "docker": True, "src": str(vm.src), "projects": [], "v1": []}
    r = vm.ok("ls")
    assert r.stdout.splitlines()[1:] == []


def test_ls_docker_down(vm):
    vm.set_docker(down=True)
    doc = ls_json(vm)
    assert doc["docker"] is False
    assert [(p["name"], p["container"], p["sidecars"]) for p in doc["projects"]] == [("alpha", None, []),
                                                                                    ("beta", None, [])]
    r = vm.ok("ls")
    assert r.stdout.splitlines()[1].split()[:2] == ["alpha", "?"]
    assert "couldn't reach rootless Docker" in r.stderr
    # docker not installed at all
    os.unlink(vm.bin / "docker")
    nodocker = vm.tmp / "nodocker"
    nodocker.mkdir()
    for d in ("/usr/bin", "/bin", "/usr/local/bin"):
        for n in os.listdir(d):
            if n != "docker" and not (nodocker / n).exists():
                os.symlink(os.path.join(d, n), nodocker / n)
    path = "%s:%s" % (vm.bin, nodocker)
    assert shutil.which("docker", path=path) is None
    assert ls_json(vm, env={"PATH": path})["docker"] is False


def test_ls_serve_states(vm):
    (vm.pdir("alpha") / "serve").write_text("")
    (vm.pdir("beta") / "serve").write_text("")
    (vm.fake / "tmux-session").write_text("")
    (vm.fake / "tmux-windows.json").write_text(json.dumps(["dashboard", "alpha", "alpha~serve", "beta"]))
    doc = ls_json(vm)
    assert [p["serve"] for p in doc["projects"]] == ["on", "stopped"]
    assert ["-L", "boxen", "list-windows", "-t", "boxen:", "-F", "#{window_name}"] in vm.tmux_calls()


@pytest.fixture
def claude_procs(vm):
    """Start fake Claude Code processes (a copy of sleep called claude) in
    given folders."""
    exe = vm.tmp / "procs" / "claude"
    exe.parent.mkdir()
    shutil.copy(shutil.which("sleep"), exe)
    procs = []

    def start(cwd):
        p = subprocess.Popen([str(exe), "60"], cwd=cwd)
        procs.append(p)
        assert wait_for(lambda: open("/proc/%d/comm" % p.pid).read().strip() == "claude")
        return p
    yield start
    for p in procs:
        p.kill()
        p.wait()


def test_ls_counts_sessions(vm, claude_procs):
    (vm.src / "alpha" / "sub").mkdir()
    claude_procs(vm.src / "alpha")
    claude_procs(vm.src / "alpha" / "sub")
    claude_procs(vm.src / "beta")
    claude_procs(vm.src / "old")                       # a v1 project: not counted
    claude_procs(vm.tmp)                               # not in a project
    os.symlink(vm.src / "beta", vm.tmp / "betalink")
    claude_procs(vm.tmp / "betalink")                  # through a link: its real folder
    doc = ls_json(vm)
    assert [(p["name"], p["sessions"]) for p in doc["projects"]] == [("alpha", 2), ("beta", 2)]
    r = vm.ok("ls")
    assert r.stdout.splitlines()[1].split()[:3] == ["alpha", "running", "2"]
    # another program isn't counted
    r = vm.fn("claude_counts | sort")
    assert r.stdout == "alpha 2\nbeta 2\n"


def test_ls_github_and_origin(vm):
    (vm.pdir("alpha") / "repos").write_text("myorg/x\nTheyCallMeBoxy/Alpha\n")
    doc = ls_json(vm)
    a = doc["projects"][0]
    assert a["repo"] == "myorg/x"
    assert [(r["repo"], r["origin"]) for r in a["repos"]] == [("myorg/x", False), ("TheyCallMeBoxy/Alpha", True)]
    # a project folder that's gone, or has no git
    shutil.rmtree(vm.src / "alpha")
    a = ls_json(vm)["projects"][0]
    assert [r["origin"] for r in a["repos"]] == [False, False]


# ------------------------------------------------------------------- serve

def test_serve_on_off(vm):
    a = str(vm.src / "alpha")
    r = vm.ok("serve", "alpha")
    assert (vm.pdir("alpha") / "serve").exists()
    assert "alpha: Remote Control server running (tmux window alpha~serve)" in r.stdout
    calls = vm.tmux_calls(clear=True)
    # no tmux server yet: a session, started with its window
    assert calls[-1] == ["-L", "boxen", "-f", os.path.join(REPO, "ui", "tmux.conf"), "new-session", "-d",
                         "-s", "boxen", "-n", "alpha~serve", "-c", a, BOXEN, "serve-run", "alpha"]
    # started again: it's running, nothing new
    vm.ok("serve", "alpha")
    assert not any(c[2] in ("new-window", "new-session") for c in vm.tmux_calls(clear=True))
    # another project, with the server up: a window
    vm.ok("serve", "beta")
    assert vm.tmux_calls()[-1] == ["-L", "boxen", "new-window", "-d", "-t", "boxen:", "-n", "beta~serve",
                                   "-c", str(vm.src / "beta"), BOXEN, "serve-run", "beta"]
    r = vm.ok("serve")
    assert r.stdout == "%-24s on\n%-24s on\n" % ("alpha", "beta")
    vm.clear_logs()
    r = vm.ok("serve", "alpha", "--off")
    assert r.stdout == "alpha: serve off\n"
    assert not (vm.pdir("alpha") / "serve").exists()
    assert ["-L", "boxen", "kill-window", "-t", "boxen:=alpha~serve"] in vm.tmux_calls()
    r = vm.ok("serve")
    assert r.stdout == "%-24s on\n" % "beta"
    # tmux never sees boxen's DOCKER_HOST
    assert {json.loads(x)["docker_host"] for x in open(vm.fake / "tmux.jsonl")} == {None}


def test_serving_reads_all_of_tmuxs_output(vm):
    """The window is found even when tmux has more to write after it (grep
    -q exiting first would break tmux's pipe, which pipefail counts)."""
    (vm.fake / "tmux-session").write_text("")
    (vm.fake / "tmux-windows.json").write_text(json.dumps(["alpha~serve"] + ["w%d" % i for i in range(300)]))
    r = vm.fn("for i in $(seq 30); do serving alpha || echo MISSED; done")
    assert r.returncode == 0 and "MISSED" not in r.stdout


def test_serve_restore_and_listing(vm):
    (vm.pdir("alpha") / "serve").write_text("")
    r = vm.ok("serve")
    assert r.stdout == "%-24s on, not running (boxen serve alpha)\n" % "alpha"
    vm.ok("serve", "--restore")
    assert [c[c.index("-n") + 1] for c in vm.tmux_calls() if "-n" in c] == ["alpha~serve"]


def test_serve_errors(vm):
    for args, msg in ((["old"], "run 'boxen migrate' first"), (["nosuch"], "no such project"),
                      (["alpha", "--on"], "usage: boxen serve [<name> [--off]]")):
        r = vm.boxen("serve", *args)
        assert r.returncode == 1 and msg in r.stderr, args
    os.unlink(vm.bin / "claude")
    r = vm.boxen("serve", "alpha", env={"PATH": str(vm.bin) + ":/usr/bin:/bin"})
    assert r.returncode == 1 and "Claude Code isn't installed" in r.stderr
    assert not (vm.pdir("alpha") / "serve").exists()


# ----------------------------------------------------------- git-credential

REQ = "protocol=https\nhost=github.com\npath=%s\n\n"
ANS = "username=x-access-token\npassword=%s\n"


def cred(vm, stdin, cwd=None, args=("get",)):
    return vm.boxen("git-credential", *args, cwd=cwd or vm.src / "alpha", input=stdin)


def test_git_credential(vm):
    tok = vm.pdir("alpha") / "token"
    tok.mkdir()
    (tok / "gh-token").write_text("DEFAULT\n")
    (tok / "gh-token.myorg").write_text("ORG\nsecond line\n")
    sub = vm.src / "alpha" / "deep"
    sub.mkdir()
    assert cred(vm, REQ % "MyOrg/x.git").stdout == ANS % "ORG"
    assert cred(vm, REQ % "other/x.git", cwd=sub).stdout == ANS % "DEFAULT"
    assert cred(vm, "protocol=https\nhost=github.com\n\n").stdout == ANS % "DEFAULT"          # no path
    assert cred(vm, "host=github.com\npath=myorg/y\n").stdout == ANS % "ORG"                  # no protocol, EOF
    assert cred(vm, REQ % "../gh-token.myorg/x").stdout == ANS % "DEFAULT"                    # not an owner
    assert cred(vm, REQ % "-/x").stdout == ANS % "DEFAULT"
    for bad in ("protocol=https\nhost=evil.com\npath=myorg/x\n\n",
                "protocol=http\nhost=github.com\npath=myorg/x\n\n",
                "protocol=https\nhost=github.com.evil.com\npath=myorg/x\n\n"):
        r = cred(vm, bad)
        assert r.returncode == 0 and r.stdout == "", bad
    for op in ("store", "erase", ""):
        r = cred(vm, REQ % "myorg/x", args=(op,) if op else ())
        assert r.returncode == 0 and r.stdout == "", op
    # outside a project, in a v1 project, in a folder that isn't one: nothing
    (vm.pdir("old") / "token").mkdir()
    (vm.pdir("old") / "token" / "gh-token").write_text("V1\n")
    (vm.src / "notaproject").mkdir()
    for cwd in (vm.tmp, vm.src, vm.src / "old", vm.src / "notaproject"):
        r = cred(vm, REQ % "myorg/x", cwd=cwd)
        assert r.returncode == 0 and r.stdout == "" and r.stderr == "", cwd
    # the physical folder decides
    os.symlink(vm.src / "alpha", vm.tmp / "alink")
    assert cred(vm, REQ % "myorg/x", cwd=vm.tmp / "alink").stdout == ANS % "ORG"
    os.symlink(vm.tmp, vm.src / "beta" / "out")
    r = cred(vm, REQ % "myorg/x", cwd=vm.src / "beta" / "out")
    assert r.stdout == ""
    # no tokens: nothing, quietly
    r = cred(vm, REQ % "myorg/x", cwd=vm.src / "beta")
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


def test_git_credential_project_option(vm):
    tok = vm.pdir("beta") / "token"
    tok.mkdir()
    (tok / "gh-token").write_text("BETA\n")
    assert cred(vm, REQ % "a/b", cwd=vm.tmp, args=("--project", "beta", "get")).stdout == ANS % "BETA"
    # --project wins over the folder
    assert cred(vm, REQ % "a/b", args=("--project", "beta", "get")).stdout == ANS % "BETA"
    for bad in ("../beta", "Beta", "beta/token", ""):
        r = cred(vm, REQ % "a/b", cwd=vm.tmp, args=("--project", bad, "get"))
        assert r.returncode == 0 and r.stdout == "", bad
    r = cred(vm, REQ % "a/b", cwd=vm.tmp, args=("--project",))
    assert r.returncode == 0 and r.stdout == ""
