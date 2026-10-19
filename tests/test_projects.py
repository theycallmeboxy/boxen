"""Project names, v1 vs v2 projects, `boxen new`, and the user commands
(up, down, rm, help) with a fake rootless Docker."""
import os
import stat

import pytest

from conftest import BOXEN, DOCKER_HOST, FORMAT_MARK, REPO, hold_lock


# ------------------------------------------------------------------ names

GOOD = ["a", "a1", "my-proj", "my_proj", "a-b-c", "x9-y_z", "a" * 40]
BAD = ["", "A", "Alpha", "-a", "a-", "_a", "a--b", "a_-b", "a" * 41, "a.b", "a/b", "a b", "a\nb", "ä", "../x"]


def test_valid_names(vm):
    for n in GOOD:
        r = vm.fn("valid_name %s && echo ok" % _q(n))
        assert r.returncode == 0 and r.stdout == "ok\n", (n, r.stderr)
    for n in BAD:
        r = vm.fn("valid_name %s && echo ok" % _q(n))
        assert r.returncode == 1 and "invalid project name" in r.stderr, n


def _q(s):
    return "'" + s.replace("'", "'\\''") + "'"


def test_reserved_names(vm):
    for n in ("all", "global", "dashboard", "ui"):
        r = vm.boxen("new", n)
        assert r.returncode == 1 and "'%s' is reserved; pick another name" % n in r.stderr
    assert vm.docker_calls() == []          # refused before anything else
    r = vm.boxen("new", "Bad")
    assert r.returncode == 1 and "invalid project name 'Bad'" in r.stderr


def test_require_project(vm):
    r = vm.boxen("up", "old")
    assert r.returncode == 1
    assert r.stderr == "boxen: old is a boxen v1 project (agent in a container); run 'boxen migrate' first\n"
    r = vm.boxen("up", "nosuch")
    assert r.returncode == 1 and r.stderr == "boxen: no such project: nosuch\n"
    r = vm.boxen("down")
    assert r.returncode == 1 and "missing project name" in r.stderr
    for cmd in (["down", "old"], ["shell", "old"], ["rm", "old", "--yes"], ["repos", "old"], ["serve", "old"],
                ["claude", "old"]):
        r = vm.boxen(*cmd)
        assert r.returncode == 1 and "run 'boxen migrate' first" in r.stderr, cmd
    # the mark must be a line of its own, exactly
    (vm.pdir("beta") / "compose.yaml").write_text(FORMAT_MARK + " \nname: x\n")
    r = vm.boxen("down", "beta")
    assert r.returncode == 1 and "boxen v1 project" in r.stderr


def test_projects_and_v1_projects(vm):
    (vm.projects / "stray").mkdir()                    # no compose.yaml: neither
    (vm.projects / ".v1-supervisor").mkdir()
    (vm.projects / ".v1-supervisor" / "compose.yaml").write_text("x\n")
    r = vm.fn("projects; echo --; v1_projects")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "alpha\nbeta\n--\nold\n"
    # no projects folder at all
    import shutil
    shutil.rmtree(vm.projects)
    r = vm.fn("projects; echo --; v1_projects")
    assert r.returncode == 0 and r.stdout == "--\n"


def test_project_of_dir(vm):
    s = vm.src
    (s / "alpha" / "deep" / "er").mkdir(parents=True)
    (s / "notaproject").mkdir()
    cases = {str(s / "alpha"): "alpha", str(s / "alpha" / "deep" / "er"): "alpha", str(s / "beta"): "beta",
             str(s): None, str(s / "old"): None, str(s / "notaproject"): None, str(vm.tmp): None,
             str(s) + "x/alpha": None, "/": None}
    for d, want in cases.items():
        r = vm.fn("project_of_dir %s" % _q(d))
        if want:
            assert r.returncode == 0 and r.stdout == want + "\n", d
        else:
            assert r.returncode == 1 and r.stdout == "", d


def test_help(vm):
    r = vm.ok("help")
    assert r.stdout.startswith("Usage: boxen <command> [args]")
    for c in ("new <name>", "run [-C dir] cmd...", "env apply", "migrate", "serve", "repos <name>"):
        assert c in r.stdout
    r = vm.boxen("frobnicate")
    assert r.returncode == 1 and "Usage: boxen" in r.stderr
    r = vm.boxen("ls", "--yaml")
    assert r.returncode == 1 and "usage: boxen ls [--json]" in r.stderr


# ------------------------------------------------------------------ Docker

def test_refuses_a_daemon_that_isnt_rootless(vm):
    vm.set_docker(rootless=False)
    r = vm.boxen("new", "gamma")
    assert r.returncode == 1 and "isn't rootless" in r.stderr and DOCKER_HOST in r.stderr
    assert not (vm.pdir("gamma")).exists()
    r = vm.boxen("up", "alpha")
    assert r.returncode == 1 and "isn't rootless" in r.stderr
    assert not any(c[0] == "compose" for c in vm.docker_calls())


def test_docker_unreachable(vm):
    vm.set_docker(down=True)
    r = vm.boxen("up", "alpha")
    assert r.returncode == 1 and "can't reach rootless Docker at %s" % DOCKER_HOST in r.stderr
    assert "sandbox" not in r.stderr
    # the socket there but not answering
    from conftest import unix_socket
    s = unix_socket(vm.run_dir / "docker.sock")
    try:
        r = vm.boxen("up", "alpha", env={"BOXEN_DOCKER_HOST": ""})
        assert r.returncode == 1
        assert r.stderr == "boxen: can't reach rootless Docker at unix://%s/docker.sock (systemctl --user status " \
                           "docker)\n" % vm.run_dir
    finally:
        s.close()


SANDBOX_HINT = ("boxen: this ran inside Claude Code's sandbox, which blocks Docker: make 'boxen run ...' (or "
                "'boxen env ...') the whole command line, with no 'cd ... &&', pipes, redirects or $(...) around it\n")


def test_docker_blocked_by_the_sandbox(vm):
    """Inside Claude Code's sandbox (SANDBOX_RUNTIME set, or the socket
    replaced by /dev/null): the hint to make boxen run the whole command."""
    vm.set_docker(down=True)
    r = vm.boxen("run", "make", cwd=vm.src / "beta", env={"SANDBOX_RUNTIME": "1"})
    assert r.returncode == 1 and r.stderr == SANDBOX_HINT
    r = vm.boxen("env", "apply", cwd=vm.src / "alpha", env={"SANDBOX_RUNTIME": "1"})
    assert r.returncode == 1 and r.stderr == SANDBOX_HINT
    os.symlink("/dev/null", vm.run_dir / "docker.sock")          # a character device where the socket was
    r = vm.boxen("up", "alpha")
    assert r.returncode == 1 and r.stderr == SANDBOX_HINT
    # docker reachable: SANDBOX_RUNTIME alone changes nothing
    vm.set_docker(down=False)
    vm.ok("up", "alpha", env={"SANDBOX_RUNTIME": "1"})


def test_docker_host_is_always_boxens(vm):
    vm.ok("up", "alpha", env={"DOCKER_HOST": "tcp://elsewhere:2375"})
    assert {c["host"] for c in vm.docker_log()} == {DOCKER_HOST}
    vm.clear_logs()
    # without BOXEN_DOCKER_HOST: the rootless socket in XDG_RUNTIME_DIR
    vm.ok("up", "alpha", env={"BOXEN_DOCKER_HOST": "", "DOCKER_HOST": "tcp://elsewhere:2375"})
    assert {c["host"] for c in vm.docker_log()} == {"unix://%s/docker.sock" % vm.run_dir}


# --------------------------------------------------------------------- new

def compose_text(vm, n):
    return (vm.pdir(n) / "compose.yaml").read_text()


def test_new_creates_everything(vm):
    r = vm.ok("new", "gamma")
    src = vm.src / "gamma"
    pdir = vm.pdir("gamma")
    # .boxen/ from the templates, placeholders filled
    df = (src / ".boxen" / "Dockerfile").read_text()
    sv = (src / ".boxen" / "services.yaml").read_text()
    assert "FROM boxen-env:latest" in df and "The build container for gamma" in df
    assert "stored as boxen.gamma.pgdata" in sv and "projects/gamma/compose.override.yaml" in sv
    for t in (df, sv):
        assert "__" not in t
    for f in ("Dockerfile", "services.yaml"):
        assert stat.S_IMODE((src / ".boxen" / f).stat().st_mode) == 0o644
    assert sorted(os.listdir(src / ".boxen")) == ["Dockerfile", "services.yaml"]     # no temp files left
    # compose.yaml: the mark, the paths, TZ
    c = compose_text(vm, "gamma")
    assert c.splitlines()[0] == FORMAT_MARK
    assert "__" not in c
    s = str(src)
    for want in ("name: boxen-gamma", "      context: %s/.boxen" % s, "    image: boxen-env-gamma:latest",
                 "    container_name: boxen.gamma.env", "    working_dir: %s" % s, "      TZ: Europe/Berlin",
                 "      BOXEN_PROJECT: gamma", "      - %s:%s" % (s, s), "    name: boxen.gamma.cache", "      - cache:/root/.cache",
                 "    hostname: gamma"):
        assert want in c.splitlines(), want
    assert "`boxen up gamma`" in (pdir / "compose.override.example.yaml").read_text()
    assert stat.S_IMODE((pdir / "token").stat().st_mode) == 0o700
    # an empty repo list (no clone url, no origin)
    repos = (pdir / "repos").read_text()
    assert "boxen repos gamma add|rm" in repos and [x for x in repos.splitlines() if not x.startswith("#")] == []
    # build, then up, with only the generated compose.yaml (services.yaml is all comments)
    cc = vm.compose_calls()
    assert [x["args"] for x in cc] == [["build", "env"], ["up", "-d", "--remove-orphans"]]
    assert all(x["files"] == [str(pdir / "compose.yaml")] for x in cc)
    calls = vm.docker_calls()
    assert calls[0][:2] == ["info", "-f"]
    assert ["compose", "--project-directory", str(pdir), "-f", str(pdir / "compose.yaml"), "build", "env"] in calls
    assert "home:" not in c and "/root\n" not in c
    # the template it came from is kept, for telling hand edits later
    assert (pdir / ".compose.template").read_text() == open(os.path.join(REPO, "templates", "compose.yaml")).read()
    assert not any(c[0] == "run" for c in calls)          # no compose config: nothing declared
    assert "gamma: build container up\n" in r.stdout
    assert "boxen claude gamma" in r.stdout
    assert not (vm.src / "gamma" / ".git").exists()


def test_new_tz_falls_back_to_utc(vm):
    from conftest import write_exe
    write_exe(str(vm.bin / "timedatectl"), "#!/bin/sh\necho 'Not/A Zone;rm'\n")
    vm.ok("new", "gamma")
    assert "      TZ: UTC" in compose_text(vm, "gamma").splitlines() or \
        "      TZ: %s" % open("/etc/timezone").read().strip() in compose_text(vm, "gamma").splitlines()


def test_new_never_overwrites_boxen_files(vm):
    d = vm.src / "delta" / ".boxen"
    d.mkdir(parents=True)
    (d / "Dockerfile").write_text("FROM boxen-env:latest\nRUN mine\n")
    (vm.src / "delta" / "code.c").write_text("int main;\n")
    vm.ok("new", "delta")
    assert (d / "Dockerfile").read_text() == "FROM boxen-env:latest\nRUN mine\n"
    assert "Sidecar services for delta" in (d / "services.yaml").read_text()       # the missing one is added
    assert (vm.src / "delta" / "code.c").exists()
    # an existing project is refused
    r = vm.boxen("new", "delta")
    assert r.returncode == 1 and "project 'delta' already exists" in r.stderr


def test_new_refuses_symlinked_boxen_dir(vm):
    (vm.src / "eps").mkdir()
    os.symlink(vm.tmp, vm.src / "eps" / ".boxen")
    r = vm.boxen("new", "eps")
    assert r.returncode != 0 and ".boxen is a symlink or not a folder; refusing" in r.stderr
    assert not (vm.tmp / "Dockerfile").exists()
    assert not vm.pdir("eps").exists()                   # cleaned up
    assert (vm.src / "eps").is_dir()                     # it was there before


def test_new_cleans_up_on_failure(vm):
    vm.set_docker(fail=["up"])
    r = vm.boxen("new", "eps")
    assert r.returncode != 0
    assert "'new eps' didn't finish" in r.stderr and "cleaning up" in r.stderr
    assert not vm.pdir("eps").exists()
    assert not (vm.src / "eps").exists()                 # boxen made it, so it goes
    assert any(c["args"][:3] == ["down", "--rmi", "all"] for c in vm.compose_calls())
    # a folder that was there before stays, with its files
    (vm.src / "zeta").mkdir()
    (vm.src / "zeta" / "keep.txt").write_text("mine\n")
    r = vm.boxen("new", "zeta")
    assert r.returncode != 0
    assert not vm.pdir("zeta").exists()
    assert (vm.src / "zeta" / "keep.txt").read_text() == "mine\n"
    # and a retry works
    vm.set_docker(fail=[])
    vm.ok("new", "zeta")
    assert FORMAT_MARK in compose_text(vm, "zeta")


def test_new_build_failure_cleans_up(vm):
    vm.set_docker(fail=["build"])
    r = vm.boxen("new", "eps")
    assert r.returncode != 0 and "building eps's build container (.boxen/Dockerfile) failed" in r.stderr
    assert "fake compose: build failed" in r.stderr
    assert not vm.pdir("eps").exists() and not (vm.src / "eps").exists()


def test_new_needs_base_image(vm):
    vm.set_docker(images=[])
    r = vm.boxen("new", "eps")
    assert r.returncode == 1 and "base image boxen-env:latest missing" in r.stderr
    assert not vm.pdir("eps").exists()


def test_new_clone(vm):
    r = vm.ok("new", "gamma", "git@github.com:TheyCallMeBoxy/gamma.git")
    assert "GitHub isn't set up" in r.stderr
    (g,) = vm.git_log()
    src = str(vm.src / "gamma")
    helper = "!%s git-credential --project gamma" % BOXEN
    assert g["argv"] == ["-c", "credential.https://github.com.helper=",
                         "-c", "credential.https://github.com.helper=" + helper,
                         "-c", "credential.https://github.com.useHttpPath=true",
                         "clone", "--", "https://github.com/TheyCallMeBoxy/gamma.git", src]
    assert g["cred"] == "quit=1\n"         # no token: git asks nobody else and fails on its own
    assert [x for x in (vm.pdir("gamma") / "repos").read_text().splitlines() if not x.startswith("#")] == \
        ["TheyCallMeBoxy/gamma"]
    assert (vm.src / "gamma" / ".boxen" / "Dockerfile").exists()
    # a non-GitHub URL must be https
    r = vm.boxen("new", "delta", "git@gitlab.com:a/b.git")
    assert r.returncode == 1 and "clone over https" in r.stderr
    vm.ok("new", "delta", "https://gitlab.com/a/b.git")
    assert vm.git_log()[-1]["argv"][-2:] == ["https://gitlab.com/a/b.git", str(vm.src / "delta")]


def test_new_clone_into_nonempty_folder_refused(vm):
    (vm.src / "eps").mkdir()
    (vm.src / "eps" / "f").write_text("x")
    r = vm.boxen("new", "eps", "https://github.com/a/b")
    assert r.returncode == 1 and "already has files; can't clone into it" in r.stderr
    assert not vm.pdir("eps").exists() and vm.git_log() == []


def test_new_clone_failure_cleans_up(vm):
    (vm.fake / "git-clone-fail").write_text("")
    r = vm.boxen("new", "eps", "https://github.com/a/b")
    assert r.returncode != 0 and "cloning https://github.com/a/b.git failed" in r.stderr
    assert "repository not found" in r.stderr
    assert not vm.pdir("eps").exists() and not (vm.src / "eps").exists()
    # an empty folder that was there stays
    (vm.src / "eps").mkdir()
    r = vm.boxen("new", "eps", "https://github.com/a/b")
    assert r.returncode != 0 and (vm.src / "eps").is_dir()


def test_new_usage(vm):
    r = vm.boxen("new")
    assert r.returncode == 1 and "usage: boxen new <name> [git-url]" in r.stderr
    r = vm.boxen("new", "a", "b", "c")
    assert r.returncode == 1 and "usage: boxen new" in r.stderr


# ---------------------------------------------------------- up, down, rm

def test_up_down(vm):
    r = vm.ok("up", "alpha")
    assert r.stdout == "alpha: build container up\n"
    pdir = vm.pdir("alpha")
    files = [str(pdir / "compose.yaml")]
    # services.yaml declares nothing now: the rendered sidecars go
    assert not (pdir / "compose.services.yaml").exists()
    # (built with the sidecars it had; they go only once the build worked)
    assert [(x["args"], x["files"]) for x in vm.compose_calls()] == [
        (["build", "env"], files + [str(pdir / "compose.services.yaml")]), (["up", "-d", "--remove-orphans"], files)]
    vm.clear_logs()
    (pdir / "compose.override.yaml").write_text("services: {}\n")
    r = vm.ok("down", "alpha")
    assert r.stdout == "alpha: stopped\n"
    assert vm.compose_calls() == [{"project": "alpha", "files": files + [str(pdir / "compose.override.yaml")],
                                   "args": ["down"]}]


def test_up_all_and_busy(vm):
    with hold_lock(vm.pdir("alpha") / ".lock"):
        r = vm.boxen("down", "alpha")
        assert r.returncode == 2 and r.stderr == "boxen: alpha is busy (another boxen command is running for it)\n"
        r = vm.boxen("down", "all")
    assert r.returncode == 2
    assert "beta: stopped" in r.stdout
    assert "skipped, busy (another boxen command was running for them): alpha" in r.stderr
    r = vm.boxen("up", "all")
    assert r.returncode == 0, r.stderr
    assert {x["project"] for x in vm.compose_calls() if x["args"][0] == "up"} == {"alpha", "beta"}


def test_rm(vm):
    r = vm.boxen("rm", "alpha")
    assert r.returncode == 1 and "rm asks first" in r.stderr
    (vm.pdir("alpha") / "serve").write_text("")
    vm.ok("rm", "alpha", "--yes")
    assert not vm.pdir("alpha").exists()
    assert (vm.src / "alpha" / ".boxen" / "Dockerfile").exists()         # the code stays
    assert vm.compose_calls()[-1]["args"] == ["down", "--rmi", "all", "-v", "--remove-orphans"]
    assert ["volume", "ls", "-q", "--filter", "label=boxen.project=alpha"] in vm.docker_calls()


def test_update(vm):
    r = vm.ok("update")
    assert "update done" in r.stdout
    calls = vm.docker_calls()
    assert ["build", "--pull", "--no-cache", "-t", "boxen-env:core", os.path.join(REPO, "image")] in calls
    # alpha has a container: rebuilt; beta has none: its image is dropped
    assert [x["project"] for x in vm.compose_calls() if x["args"] == ["build", "env"]] == ["alpha"]
    assert ["image", "rm", "boxen-env-beta:latest"] in calls
    assert vm.boxen("update", "alpha").returncode == 1


def test_bin_dir_has_no_stray_files():
    """Everything install.sh links or chmods is there."""
    for f in ("boxen", "boxen-gh", "boxen-services", "boxen-gitscan", "boxen-carry"):
        assert os.access(os.path.join(REPO, "bin", f), os.X_OK), f
    assert os.access(os.path.join(REPO, "image", "boxen-bg"), os.X_OK)


@pytest.mark.parametrize("cmd", [["shell", "alpha"], ["claude", "alpha"]])
def test_needs_a_terminal(vm, cmd):
    r = vm.boxen(*cmd)
    assert r.returncode == 1 and "needs a terminal" in r.stderr


# ------------------------------------------------- compose.yaml, self-update

def test_write_compose_keeps_hand_edits(vm):
    """The template a compose.yaml came from is kept in .compose.template:
    a template change replaces an unedited compose.yaml quietly, and one
    edited by hand is kept as compose.yaml.old."""
    pdir = vm.pdir("beta")
    tmpl = os.path.join(REPO, "templates", "compose.yaml")
    (pdir / "compose.yaml").unlink()
    r = vm.fn("write_compose beta")
    assert r.returncode == 0 and r.stdout == "", r.stderr
    fresh = (pdir / "compose.yaml").read_text()
    assert fresh.startswith(FORMAT_MARK + "\n") and not (pdir / "compose.yaml.old").exists()
    assert (pdir / ".compose.template").read_text() == open(tmpl).read()
    # unchanged: nothing kept
    vm.fn("write_compose beta")
    assert not (pdir / "compose.yaml.old").exists()
    # a template change, compose.yaml as boxen wrote it from the old template: replaced quietly
    old_t = open(tmpl).read() + "# old line for __NAME__\n"
    (pdir / ".compose.template").write_text(old_t)
    r = vm.fn("render %s beta > %s; write_compose beta" % (pdir / ".compose.template", pdir / "compose.yaml"))
    assert r.returncode == 0 and r.stdout == "", r.stderr
    assert (pdir / "compose.yaml").read_text() == fresh
    assert not (pdir / "compose.yaml.old").exists()
    assert (pdir / ".compose.template").read_text() == open(tmpl).read()
    # edited by hand (with or without a template change): kept as compose.yaml.old
    for template in (old_t, None):
        (pdir / "compose.yaml.old").unlink(missing_ok=True)
        if template:
            (pdir / ".compose.template").write_text(template)
        (pdir / "compose.yaml").write_text(fresh + "# my edit\n")
        r = vm.fn("write_compose beta")
        assert "beta: compose.yaml had your edits; kept as compose.yaml.old" in r.stdout
        assert (pdir / "compose.yaml.old").read_text() == fresh + "# my edit\n"
        assert (pdir / "compose.yaml").read_text() == fresh
    # no template kept (a project from before): any difference counts as an edit
    (pdir / ".compose.template").unlink()
    (pdir / "compose.yaml.old").unlink()
    (pdir / "compose.yaml").write_text(fresh + "# x\n")
    assert "had your edits" in vm.fn("write_compose beta").stdout
    assert not [f for f in os.listdir(pdir) if f.startswith(".compose.") and f != ".compose.template"]   # no temp files
    # a template it can't render leaves everything as it was
    bad = vm.tmp / "repo"
    (bad / "templates").mkdir(parents=True)
    (bad / "templates" / "compose.yaml").write_text("x: __NOPE__\n")
    r = vm.fn("BOXEN_REPO=%s; write_compose beta || echo failed" % bad)
    assert "failed" in r.stdout and (pdir / "compose.yaml").read_text() == fresh
    assert not [f for f in os.listdir(pdir) if f.startswith(".compose.") and f != ".compose.template"]
    # a kept template this boxen can't render (a placeholder it doesn't know
    # any more): its compose.yaml counts as edited, nothing breaks
    (pdir / ".compose.template").write_text("x: __GONE__\n")
    (pdir / "compose.yaml").write_text("x: something\n")
    r = vm.fn("write_compose beta && echo ok")
    assert r.stdout.endswith("ok\n") and "had your edits" in r.stdout
    assert (pdir / "compose.yaml").read_text() == fresh


def test_setup_rewrites_every_compose_yaml(vm):
    """boxen setup (which install.sh and self-update run) rewrites every v2
    project's compose.yaml; a busy one is skipped with a warning."""
    for n in ("alpha", "beta"):
        (vm.pdir(n) / "compose.yaml").unlink()
        assert vm.fn("write_compose %s" % n).returncode == 0
    (vm.pdir("beta") / "compose.yaml").write_text(FORMAT_MARK + "\n# mine\n")
    r = vm.ok("setup")
    assert "beta: compose.yaml had your edits; kept as compose.yaml.old" in r.stdout
    assert (vm.pdir("beta") / "compose.yaml.old").read_text() == FORMAT_MARK + "\n# mine\n"
    assert not (vm.pdir("alpha") / "compose.yaml.old").exists()
    assert "container_name: boxen.beta.env" in (vm.pdir("beta") / "compose.yaml").read_text()
    assert (vm.pdir("old") / "compose.yaml").read_text().startswith("name: boxen-old")      # v1: untouched
    (vm.pdir("beta") / "compose.yaml").write_text(FORMAT_MARK + "\n# mine again\n")
    with hold_lock(vm.pdir("beta") / ".lock"):
        r = vm.ok("setup")
    assert "beta: couldn't update its compose.yaml (busy?)" in r.stderr
    assert (vm.pdir("beta") / "compose.yaml").read_text() == FORMAT_MARK + "\n# mine again\n"


def test_render_refuses_unknown_placeholders(vm):
    t = vm.tmp / "t.yaml"
    t.write_text("name: __NAME__\nsrc: __SRC__\ntz: __TZ__\nx: __NEWTHING__\n")
    r = vm.fn("render %s alpha" % t)
    assert r.returncode == 1 and "has unfilled placeholder __NEWTHING__" in r.stderr and r.stdout == ""
    t.write_text("name: __NAME__ | & \\ __SRC__\n")
    r = vm.fn("render %s alpha" % t)
    assert r.stdout == "name: alpha | & \\ %s\n" % (vm.src / "alpha")


def lock_holders(path):
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


def test_self_update_leaves_no_lock_behind(vm):
    """self-update runs the new install.sh, which may leave a daemon
    running: it must not inherit the global lock. The new installer's setup
    rewrites every compose.yaml; the ones with a container are rebuilt,
    since templates/compose.yaml changed."""
    import shutil
    import subprocess
    work = vm.tmp / "work"
    shutil.copytree(REPO, work, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    g = ["git", "-C", str(work), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["commit", "-qm", "wip", "--allow-empty"], check=True)
    branch = subprocess.run(g + ["rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
    up = vm.tmp / "up.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(up)], check=True)
    subprocess.run(g + ["remote", "remove", "origin"], capture_output=True)
    subprocess.run(g + ["remote", "add", "origin", str(up)], check=True)
    subprocess.run(g + ["fetch", "-q", "origin"], check=True)
    subprocess.run(g + ["branch", "-q", "--set-upstream-to=origin/" + branch], check=True)
    other = vm.tmp / "other"
    subprocess.run(["git", "clone", "-q", str(up), str(other)], check=True)
    pidfile = vm.fake / "daemon.pid"
    # like the real one: boxen setup, and (the case at hand) something left running
    (other / "install.sh").write_text('#!/bin/bash\nset -e\n"$(dirname "$0")/bin/boxen" setup\n'
                                      "nohup sleep 60 >/dev/null 2>&1 &\necho $! > %s\ndisown\n" % pidfile)
    with open(other / "templates" / "compose.yaml", "a") as f:
        f.write("# a new line for __NAME__\n")
    go = ["git", "-C", str(other), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(go + ["commit", "-qam", "new installer"], check=True)
    subprocess.run(go + ["push", "-q", "origin", branch], check=True)
    # alpha's compose.yaml is as boxen wrote it; beta's was edited by hand
    (vm.pdir("alpha") / "compose.yaml").unlink()
    assert vm.fn("write_compose alpha").returncode == 0
    (vm.pdir("beta") / "compose.yaml").write_text(FORMAT_MARK + "\n# mine\n")
    r = subprocess.run([str(work / "bin" / "boxen"), "self-update"], env=vm.env, capture_output=True, text=True,
                       timeout=120)
    try:
        assert r.returncode == 0, r.stdout + r.stderr
        assert "self-update done" in r.stdout and "new installer" in r.stdout
        daemon = int(pidfile.read_text())
        assert os.path.exists("/proc/%d" % daemon)
        lock = vm.bhome / ".global.lock"
        assert lock_holders(lock) == []
        with hold_lock(lock):
            pass
        for n in ("alpha", "beta"):
            assert (vm.pdir(n) / "compose.yaml").read_text().endswith("# a new line for %s\n" % n)
        assert (vm.pdir("beta") / "compose.yaml.old").read_text() == FORMAT_MARK + "\n# mine\n"
        assert not (vm.pdir("alpha") / "compose.yaml.old").exists()
        # the template changed: alpha (which has a container) is rebuilt, beta isn't
        assert [x["project"] for x in vm.compose_calls() if x["args"] == ["build", "env"]] == ["alpha"]
        r = vm.boxen("self-update")         # (this checkout isn't a clone with upstream: fails, not busy)
        assert "busy" not in r.stderr
    finally:
        try:
            os.kill(int(pidfile.read_text()), 15)
        except (OSError, ValueError):
            pass


# --------------------------------------------------------- with a terminal

def with_tty(vm, *args, env=None):
    import shlex
    import subprocess
    return subprocess.run(["script", "-qec", shlex.join([BOXEN] + list(args)), "/dev/null"],
                          env=dict(vm.env, **(env or {})), capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=30)


def test_shell(vm):
    r = with_tty(vm, "shell", "alpha")
    assert r.returncode == 0, r.stdout
    assert vm.docker_calls()[-1] == ["exec", "-it", "-w", str(vm.src / "alpha"), "-e", "TERM=xterm-test",
                                     "boxen.alpha.env", "bash", "-l"]
    r = with_tty(vm, "shell", "alpha", "x")
    assert r.returncode == 1 and "usage: boxen shell <name>" in r.stdout


CLAUDE_SH = ["sh", "-c", 'claude "$@" || { printf "\\nclaude exited (%s); press Enter " "$?"; read -r _; }', "claude"]


def test_claude_session_in_tmux(vm):
    a = str(vm.src / "alpha")
    conf = os.path.join(REPO, "ui", "tmux.conf")
    r = with_tty(vm, "claude", "alpha", "--resume", "it's")
    assert r.returncode == 0, r.stdout
    assert vm.tmux_calls(clear=True)[-1] == ["-L", "boxen", "-f", conf, "new-session", "-s", "boxen", "-n", "alpha",
                                             "-c", a] + CLAUDE_SH + ["--resume", "it's"]
    # a session there already: a new window, then attach
    (vm.fake / "tmux-session").write_text("")
    with_tty(vm, "claude", "alpha")
    calls = vm.tmux_calls(clear=True)
    assert calls[-2:] == [["-L", "boxen", "new-window", "-t", "boxen:", "-n", "alpha", "-c", a] + CLAUDE_SH,
                          ["-L", "boxen", "attach-session", "-t", "boxen"]]
    # inside boxen's tmux: just the window
    with_tty(vm, "claude", "alpha", env={"TMUX": "/tmp/tmux-0/boxen,1,0"})
    assert vm.tmux_calls() == [["-L", "boxen", "new-window", "-t", "boxen:", "-n", "alpha", "-c", a] + CLAUDE_SH]


def test_claude_window_command(vm, tmp_path):
    """The window's command: claude's arguments pass through; a failing
    claude leaves its exit code on screen until Enter."""
    from conftest import write_exe
    fake = tmp_path / "fb"
    fake.mkdir()
    write_exe(str(fake / "claude"), '#!/bin/sh\nprintf "[%s]" "$@"; echo; exit ${CLAUDE_RC:-0}\n')
    env = dict(vm.env, PATH="%s:%s" % (fake, vm.env["PATH"]))
    import subprocess
    r = subprocess.run(CLAUDE_SH + ["a b", "$X"], env=env, capture_output=True, text=True, input="")
    assert r.returncode == 0 and r.stdout == "[a b][$X]\n"
    r = subprocess.run(CLAUDE_SH, env=dict(env, CLAUDE_RC="3"), capture_output=True, text=True, input="\n")
    assert r.stdout == "[]\n\nclaude exited (3); press Enter "
