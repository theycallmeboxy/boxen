"""`boxen run` and `boxen env`: the commands agents use, acting on the
project of the folder they're run in."""
import json
import os
import re
import signal
import shlex
import subprocess
import time

import pytest

from conftest import BOXEN, group_members, hold_lock, wait_for

EXEC_PREFIX = ["bash", "-lc", 'exec "$@"', "boxen-run"]


def execs(vm):
    return [c for c in vm.docker_calls() if c[0] == "exec"]


# --------------------------------------------------------------------- run

def test_run_refuses_outside_a_project(vm):
    (vm.src / "notaproject").mkdir()
    for cwd in (vm.tmp, vm.src, vm.src / "notaproject", vm.src / "old", vm.home):
        r = vm.boxen("run", "echo", "hi", cwd=cwd)
        assert r.returncode == 1, cwd
        assert "not in a boxen project folder (~/src/<project>)" in r.stderr
    assert execs(vm) == []


def supervised(call):
    """(argv without the run id, the id) of a `boxen run` exec without a
    terminal: docker exec ... CONTAINER boxen-exec ID bash -lc ..."""
    i = call.index("boxen-exec")
    return call[:i + 1] + call[i + 2:], call[i + 1]


def test_run_maps_the_folder(vm):
    sub = vm.src / "alpha" / "sub dir"
    sub.mkdir()
    r = vm.ok("run", "make", "-j4", cwd=sub)
    assert r.stdout == ""
    (call,) = execs(vm)
    argv, rid = supervised(call)
    assert argv == ["exec", "-i", "-w", str(sub), "-e", "TERM=xterm-test", "boxen.alpha.env", "boxen-exec"] \
        + EXEC_PREFIX + ["make", "-j4"]
    assert re.fullmatch("[0-9a-f]{24}", rid)
    # through a symlink: the physical path; a new id each time
    os.symlink(sub, vm.tmp / "link")
    vm.clear_logs()
    vm.ok("run", "true", cwd=vm.tmp / "link")
    assert execs(vm)[0][3] == str(sub)
    assert supervised(execs(vm)[0])[1] != rid
    # no TERM: dumb
    vm.clear_logs()
    env = dict(vm.env)
    del env["TERM"]
    subprocess.run([BOXEN, "run", "true"], env=env, cwd=sub, check=True, capture_output=True)
    assert execs(vm)[0][5] == "TERM=dumb"


def test_run_arguments_survive(vm):
    """The exec argv, run for real by the fake (through image/boxen-exec and
    bash -lc 'exec "$@"'): spaces, quotes, $ and globs reach the command as
    they were, and so do stdin, the exit code and the folder."""
    (vm.fake / "exec-local").write_text("")
    args = ["a b", "it's", 'say "hi"', "$HOME", "*", "", "-x", "back\\slash", "new\nline"]
    r = vm.boxen("run", "printf", "[%s]", *args, cwd=vm.src / "alpha")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "".join("[%s]" % a for a in args)
    r = vm.boxen("run", "sh", "-c", "exit 7", cwd=vm.src / "alpha")
    assert r.returncode == 7
    r = vm.boxen("run", "pwd", cwd=vm.src / "alpha")
    assert r.stdout == str(vm.src / "alpha") + "\n"
    # stdin reaches the command, though docker exec runs as a background job
    r = vm.boxen("run", "sh", "-c", "tr a-z A-Z; echo rc=$?", cwd=vm.src / "alpha", input="hello\nthere\n")
    assert r.returncode == 0 and r.stdout == "HELLO\nTHERE\nrc=0\n", r.stderr
    # each run's id file is gone after it exits
    assert os.listdir(vm.fake / "exec") == []


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_run_interrupted_kills_the_command(vm, sig):
    """Claude interrupts `boxen run` with a signal: the client calls
    boxen-exec --kill with its run id, and the command's whole process group
    in the container ends; the client exits 130."""
    (vm.fake / "exec-local").write_text("")
    p = subprocess.Popen([BOXEN, "run", "sh", "-c", "sleep 60 & sleep 60; wait"], env=vm.env,
                         cwd=vm.src / "alpha", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    d = vm.fake / "exec"

    def pid_file():
        try:
            return [f for f in os.listdir(d) if (d / f).read_text().strip()]
        except OSError:
            return []
    try:
        assert wait_for(pid_file)
        (rid,) = os.listdir(d)
        pgid = int((d / rid).read_text())
        assert wait_for(lambda: len(group_members(pgid)) >= 3)        # sh, two sleeps
        p.send_signal(sig)
        out, err = p.communicate(timeout=20)
        assert p.returncode == 130, err
        assert ["exec", "boxen.alpha.env", "boxen-exec", "--kill", rid] in execs(vm)
        assert wait_for(lambda: group_members(pgid) == [])
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
        for f in pid_file():
            try:
                os.killpg(int((d / f).read_text()), 9)
            except (OSError, ValueError):
                pass


def test_run_interrupted_while_docker_waits(vm):
    """docker exec still waiting (nothing runs: the fake sleeps): the --kill
    call goes out with the id that run used."""
    from conftest import write_exe
    real = vm.bin / "docker"
    os.rename(real, vm.bin / "docker.real")
    write_exe(str(real), '#!/bin/sh\n"$(dirname "$0")/docker.real" "$@"\n'
                         'case "$1 $8" in "exec boxen-exec") exec sleep 30 ;; esac\n')
    p = subprocess.Popen([BOXEN, "run", "make"], env=vm.env, cwd=vm.src / "alpha", stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert wait_for(lambda: len(execs(vm)) == 1)
    time.sleep(0.3)
    p.terminate()
    p.communicate(timeout=20)
    assert p.returncode == 130
    first, kill = execs(vm)
    assert kill == ["exec", "boxen.alpha.env", "boxen-exec", "--kill", supervised(first)[1]]


def test_run_dash_c(vm):
    a = vm.src / "alpha"
    (a / "sub" / "deeper").mkdir(parents=True)
    os.symlink(vm.tmp, a / "out")                  # a link out of the project
    vm.ok("run", "-C", "sub", "ls", cwd=a)
    vm.ok("run", "-Csub/deeper", "ls", cwd=a)
    vm.ok("run", "-C", "..", "ls", cwd=a / "sub")
    vm.ok("run", "-C", str(a / "sub"), "ls", cwd=a)
    vm.ok("run", "-C", ".", "ls", cwd=a)
    assert [c[3] for c in execs(vm)] == [str(a / "sub"), str(a / "sub" / "deeper"), str(a), str(a / "sub"), str(a)]
    vm.clear_logs()
    for wd in ("../beta", "..", str(vm.src / "beta"), "/etc", "/", "sub/../../beta", "out", str(vm.src)):
        r = vm.boxen("run", "-C", wd, "ls", cwd=a)
        assert r.returncode == 1 and "is outside this project (%s)" % a in r.stderr, wd
    r = vm.boxen("run", "-C", "nosuch", "ls", cwd=a)
    assert r.returncode == 1 and "no such folder: %s/nosuch" % a in r.stderr
    r = vm.boxen("run", "-C", cwd=a)
    assert r.returncode == 1 and "-C needs a folder" in r.stderr
    assert execs(vm) == []


def test_run_usage(vm):
    a = vm.src / "alpha"
    r = vm.boxen("run", cwd=a)
    assert r.returncode == 1 and "usage: boxen run [-C dir] cmd..." in r.stderr
    r = vm.boxen("run", "-x", cwd=a)
    assert r.returncode == 1 and "unknown option '-x'" in r.stderr
    vm.ok("run", "--", "-x", cwd=a)
    assert execs(vm)[-1][-1] == "-x"
    r = vm.ok("run", "--help", cwd=vm.tmp)                # help works anywhere
    assert "Usage: boxen" in r.stdout
    r = vm.boxen("run", "-d", cwd=a)
    assert r.returncode == 1 and "usage: boxen run -d NAME cmd..." in r.stderr
    r = vm.boxen("run", "-d", "web", cwd=a)
    assert r.returncode == 1 and "usage: boxen run -d NAME cmd..." in r.stderr
    r = vm.boxen("run", "--stop", "web", "x", cwd=a)
    assert r.returncode == 1 and "usage: boxen run --stop NAME" in r.stderr
    r = vm.boxen("run", "--ps", "x", cwd=a)
    assert r.returncode == 1 and "usage: boxen run --ps" in r.stderr
    # usage errors come before anything starts: beta isn't running
    vm.clear_logs()
    for args in ([], ["-d", "web"], ["--stop", "web", "x"], ["--ps", "x"]):
        r = vm.boxen("run", *args, cwd=vm.src / "beta")
        assert r.returncode == 1 and "usage: boxen run" in r.stderr, args
    assert vm.docker_calls() == []


def test_run_background_commands(vm):
    a = vm.src / "alpha"
    (a / "web").mkdir()
    c = "boxen.alpha.env"
    vm.ok("run", "-d", "web", "npm", "run", "dev server", cwd=a / "web")
    vm.ok("run", "-C", "web", "-d", "api", "make", cwd=a)
    vm.ok("run", "-d", "api2", "-C", "web", "make", cwd=a)
    vm.ok("run", "--logs", "web", "-f", "--tail", "5", cwd=a)
    vm.ok("run", "--stop", "web", cwd=a)
    vm.ok("run", "--ps", cwd=a)
    bg = ["bash", "-lc", 'exec boxen-bg start "$@"', "boxen-run"]
    assert execs(vm) == [
        ["exec", "-i", "-w", str(a / "web"), c] + bg + ["web", "npm", "run", "dev server"],
        ["exec", "-i", "-w", str(a / "web"), c] + bg + ["api", "make"],
        ["exec", "-i", "-w", str(a / "web"), c] + bg + ["api2", "make"],
        ["exec", "-i", c, "boxen-bg", "logs", "web", "-f", "--tail", "5"],
        ["exec", "-i", c, "boxen-bg", "stop", "web"],
        ["exec", "-i", c, "boxen-bg", "ps"],
    ]


def test_run_with_a_terminal(vm):
    r = subprocess.run(["script", "-qec", shlex.join([BOXEN, "run", "true"]), "/dev/null"], env=vm.env,
                       cwd=vm.src / "alpha", capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
    assert r.returncode == 0, r.stdout
    assert execs(vm)[0][:2] == ["exec", "-it"]


def test_run_starts_the_container(vm):
    """beta isn't running: boxen run starts it (under its lock, output on
    stderr), then runs the command."""
    (vm.fake / "exec-local").write_text("")
    r = vm.boxen("run", "echo", "out", cwd=vm.src / "beta")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "out\n"
    assert "boxen: starting beta's build container..." in r.stderr and "beta: build container up" in r.stderr
    assert [x["args"] for x in vm.compose_calls()] == [["build", "env"], ["up", "-d", "--remove-orphans"]]
    calls = vm.docker_calls()
    assert calls.index(["inspect", "-f", "{{.State.Running}}", "boxen.beta.env"]) < \
        [i for i, c in enumerate(calls) if c[0] == "compose"][0]
    # running now: no compose at all the next time
    vm.clear_logs()
    vm.ok("run", "true", cwd=vm.src / "beta")
    assert vm.compose_calls() == []


def test_run_waits_for_the_lock(vm):
    lock = vm.pdir("beta") / ".lock"
    f = hold_lock(lock)
    try:
        p = subprocess.Popen([BOXEN, "run", "true"], env=vm.env, cwd=vm.src / "beta",
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(1)
        assert p.poll() is None                 # waiting
        assert vm.compose_calls() == []
    finally:
        f.close()
    out, err = p.communicate(timeout=30)
    assert p.returncode == 0, err
    assert [x["args"] for x in vm.compose_calls()] == [["build", "env"], ["up", "-d", "--remove-orphans"]]


def test_run_start_failure(vm):
    vm.set_docker(fail=["up"])
    r = vm.boxen("run", "true", cwd=vm.src / "beta")
    assert r.returncode != 0 and "starting beta failed" in r.stderr
    assert execs(vm) == []


# --------------------------------------------------------------------- env

PG_CONFIG = {
    "name": "w", "networks": {"default": {"name": "w_default"}},
    "services": {"postgres": {"image": "postgres:16", "networks": {"default": None},
                              "environment": {"POSTGRES_PASSWORD": "dev$x"},
                              "volumes": [{"type": "volume", "source": "pgdata",
                                           "target": "/var/lib/postgresql/data", "volume": {}}]}},
    "volumes": {"pgdata": {"name": "w_pgdata"}}}
PG_YAML = """services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_PASSWORD: dev$x
    volumes:
      - pgdata:/var/lib/postgresql/data
volumes:
  pgdata: {}
"""


def test_env_apply(vm):
    a = vm.src / "alpha"
    r = vm.ok("env", "apply", cwd=a / ".boxen")
    assert r.stdout.endswith("alpha: environment applied\n")
    assert [x["args"] for x in vm.compose_calls()] == [["build", "env"], ["up", "-d", "--remove-orphans"]]
    assert not (vm.pdir("alpha") / "compose.services.yaml").exists()      # nothing declared now
    r = vm.boxen("env", "apply", "now", cwd=a)
    assert r.returncode == 1 and "usage: boxen env apply" in r.stderr
    r = vm.boxen("env", cwd=a)
    assert r.returncode == 1 and "usage: boxen env apply | show | logs" in r.stderr
    r = vm.boxen("env", "apply", cwd=vm.tmp)
    assert r.returncode == 1 and "not in a boxen project folder" in r.stderr


def test_env_apply_with_sidecars(vm):
    a = vm.src / "alpha"
    pdir = vm.pdir("alpha")
    (a / ".boxen" / "services.yaml").write_text(PG_YAML)
    vm.set_docker(compose_config=PG_CONFIG)
    r = vm.ok("env", "apply", cwd=a)
    assert "alpha: build container up; sidecars: postgres" in r.stdout
    (run,) = [c for c in vm.docker_log() if c["argv"][0] == "run"]
    argv = run["argv"]
    w = argv[argv.index("-v") + 1][:-len(":/w:ro")]
    assert argv == ["run", "--rm", "--network", "none", "-v", w + ":/w:ro", "-w", "/w",
                    "-v", str(vm.fake / "docker-compose") + ":/compose:ro", "boxen-env:latest",
                    "/compose", "-f", "services.yaml", "config", "--format", "json", "--no-interpolate"]
    assert run["services_yaml"] == PG_YAML and run["w_files"] == ["services.yaml"]   # only a copy of the file
    assert not os.path.exists(w)                                                     # and it's cleaned up
    doc = json.loads((pdir / "compose.services.yaml").read_text())
    pg = doc["services"]["postgres"]
    assert pg["container_name"] == "boxen.alpha.postgres"
    assert pg["volumes"] == [{"type": "volume", "source": "boxen.alpha.pgdata", "target": "/var/lib/postgresql/data"}]
    assert pg["environment"] == {"POSTGRES_PASSWORD": "dev$$x"}
    assert doc["volumes"] == {"boxen.alpha.pgdata": {"name": "boxen.alpha.pgdata"}}
    files = [str(pdir / "compose.yaml"), str(pdir / "compose.services.yaml")]
    assert all(x["files"] == files for x in vm.compose_calls())
    # sidecars gone from services.yaml: the rendered file goes too
    vm.clear_logs()
    (a / ".boxen" / "services.yaml").write_text("services: {}\nvolumes:\n")
    vm.ok("env", "apply", cwd=a)
    assert not (pdir / "compose.services.yaml").exists()
    assert not any(c[0] == "run" for c in vm.docker_calls())


def test_failed_build_keeps_the_sidecars(vm):
    """The new sidecars are staged (compose.services.yaml.new) and put in
    place only once the build worked: a failed build leaves the running
    definition as it was, and so does one that would remove every sidecar."""
    a = vm.src / "alpha"
    pdir = vm.pdir("alpha")
    before = (pdir / "compose.services.yaml").read_text()           # db
    (a / ".boxen" / "services.yaml").write_text(PG_YAML)
    vm.set_docker(compose_config=PG_CONFIG, fail=["build"])
    r = vm.boxen("env", "apply", cwd=a)
    assert r.returncode == 1 and "building alpha's build container (.boxen/Dockerfile) failed" in r.stderr
    assert (pdir / "compose.services.yaml").read_text() == before
    assert [x["args"] for x in vm.compose_calls()] == [["build", "env"]]          # no up
    # no sidecars any more, and the build fails: still the old file
    vm.clear_logs()
    (a / ".boxen" / "services.yaml").write_text("# none\n")
    r = vm.boxen("env", "apply", cwd=a)
    assert r.returncode == 1 and (pdir / "compose.services.yaml").read_text() == before
    # the build works: now it goes, and nothing is left staged
    vm.set_docker(fail=[])
    vm.ok("env", "apply", cwd=a)
    assert not (pdir / "compose.services.yaml").exists()
    assert sorted(f for f in os.listdir(pdir) if f.startswith("compose.services")) == []
    # and with sidecars: in place before up, which uses them
    vm.clear_logs()
    (a / ".boxen" / "services.yaml").write_text(PG_YAML)
    vm.set_docker(compose_config=PG_CONFIG)
    vm.ok("env", "apply", cwd=a)
    build, up = vm.compose_calls()
    assert build["files"] == [str(pdir / "compose.yaml")]
    assert up["files"] == [str(pdir / "compose.yaml"), str(pdir / "compose.services.yaml")]
    assert "postgres" in json.loads((pdir / "compose.services.yaml").read_text())["services"]
    assert not (pdir / "compose.services.yaml.new").exists()


def test_rejected_services_leave_nothing_staged(vm):
    a = vm.src / "alpha"
    pdir = vm.pdir("alpha")
    (pdir / "compose.services.yaml.new").write_text("stale\n")            # from an earlier failure
    (a / ".boxen" / "services.yaml").write_text(PG_YAML)
    bad = json.loads(json.dumps(PG_CONFIG))
    bad["services"]["postgres"]["privileged"] = True
    vm.set_docker(compose_config=bad)
    r = vm.boxen("env", "apply", cwd=a)
    assert r.returncode == 1 and "privileged isn't allowed" in r.stderr
    assert not (pdir / "compose.services.yaml.new").exists()


def test_env_apply_rejects_bad_services(vm):
    a = vm.src / "alpha"
    pdir = vm.pdir("alpha")
    before = (pdir / "compose.services.yaml").read_text()
    (a / ".boxen" / "services.yaml").write_text("services:\n  x:\n    image: y\n    ports: ['80:80']\n")
    bad = json.loads(json.dumps(PG_CONFIG))
    bad["services"]["postgres"]["ports"] = [{"target": 80, "published": "80"}]
    bad["services"]["env"] = {"image": "x"}
    vm.set_docker(compose_config=bad)
    r = vm.boxen("env", "apply", cwd=a)
    assert r.returncode == 1
    assert "services.yaml: postgres: ports isn't allowed in a sidecar" in r.stderr
    assert "services.yaml: env: 'env' is this project's own build container" in r.stderr
    assert "boxen: alpha: services.yaml rejected; nothing changed" in r.stderr
    assert "nothing changed: fix the problem above and run boxen env apply again" in r.stderr
    assert vm.compose_calls() == []
    assert (pdir / "compose.services.yaml").read_text() == before
    # compose itself can't parse it
    vm.set_docker(compose_error="yaml: line 3: mapping values are not allowed here")
    r = vm.boxen("env", "apply", cwd=a)
    assert r.returncode == 1
    assert "services.yaml: yaml: line 3: mapping values are not allowed here" in r.stderr
    assert vm.compose_calls() == []


def test_env_refuses_symlinks(vm):
    a = vm.src / "alpha"
    outside = vm.tmp / "outside"
    outside.mkdir()
    (outside / "Dockerfile").write_text("FROM evil\n")
    (outside / "services.yaml").write_text(PG_YAML)
    vm.set_docker(compose_config=PG_CONFIG)
    # .boxen/services.yaml a link
    (a / ".boxen" / "services.yaml").unlink()
    os.symlink(outside / "services.yaml", a / ".boxen" / "services.yaml")
    r = vm.boxen("env", "apply", cwd=a)
    assert r.returncode == 1 and "alpha: .boxen/services.yaml is a symlink or not a plain file; refusing" in r.stderr
    # .boxen/Dockerfile a link
    (a / ".boxen" / "services.yaml").unlink()
    (a / ".boxen" / "Dockerfile").unlink()
    os.symlink(outside / "Dockerfile", a / ".boxen" / "Dockerfile")
    r = vm.boxen("env", "apply", cwd=a)
    assert r.returncode == 1 and "alpha: .boxen/Dockerfile is a symlink or not a plain file; refusing" in r.stderr
    # a folder called Dockerfile
    (a / ".boxen" / "Dockerfile").unlink()
    (a / ".boxen" / "Dockerfile").mkdir()
    r = vm.boxen("env", "apply", cwd=a)
    assert r.returncode == 1 and ".boxen/Dockerfile is a symlink or not a plain file" in r.stderr
    # .boxen itself a link
    import shutil
    shutil.rmtree(a / ".boxen")
    os.symlink(outside, a / ".boxen")
    r = vm.boxen("env", "apply", cwd=a)
    assert r.returncode == 1 and "alpha: .boxen is a symlink or not a folder; refusing" in r.stderr
    # no Dockerfile at all
    os.unlink(a / ".boxen")
    (a / ".boxen").mkdir()
    r = vm.boxen("env", "apply", cwd=a)
    assert r.returncode == 1 and "alpha: .boxen/Dockerfile is missing" in r.stderr
    assert vm.compose_calls() == []
    assert not any(c[0] == "run" for c in vm.docker_calls())
    # the same checks guard boxen up
    shutil.rmtree(a / ".boxen")
    os.symlink(outside, a / ".boxen")
    r = vm.boxen("up", "alpha")
    assert r.returncode != 0 and ".boxen is a symlink" in r.stderr and vm.compose_calls() == []


def test_env_read(vm):
    """env_read: the file's contents; 1 when it isn't there, 2 refused."""
    r = vm.fn("env_read alpha services.yaml")
    assert r.returncode == 0 and r.stdout == "# nothing\nservices:\n"
    r = vm.fn("env_read alpha nosuch || echo rc=$?")
    assert r.stdout == "rc=1\n"
    os.symlink("/etc/hostname", vm.src / "alpha" / ".boxen" / "link")
    r = vm.fn("env_read alpha link || echo rc=$?")
    assert r.stdout == "rc=2\n" and "is a symlink" in r.stderr
    # BOXEN_SRC through a link: realpath'd at start, so still fine
    os.symlink(vm.src, vm.tmp / "srclink")
    r = vm.fn("env_read alpha services.yaml", env={"BOXEN_SRC": str(vm.tmp / "srclink")})
    assert r.returncode == 0 and "services:" in r.stdout


def test_env_show(vm):
    vm.container("boxen.alpha.env", "boxen-alpha", image="boxen-env-alpha:latest", status="Up 3 hours")
    (vm.pdir("alpha") / "repos").write_text("theycallmeboxy/alpha\n")
    r = vm.ok("env", "show", cwd=vm.src / "alpha")
    lines = r.stdout.splitlines()
    assert lines[0] == "alpha: %s" % (vm.src / "alpha")
    assert lines[1] == "Containers:"
    assert "    boxen.alpha.env   boxen-env-alpha:latest   Up 3 hours" in lines
    assert any(x.startswith("Override: none") for x in lines)
    assert "    theycallmeboxy/alpha                     run boxen github  (origin)" in lines
    assert lines[-1] == "Build environment: %s/Dockerfile, %s/services.yaml" % ((vm.src / "alpha" / ".boxen",) * 2)
    r = vm.ok("env", "show", cwd=vm.src / "beta")
    assert "    none (boxen run starts the build container)" in r.stdout


def test_env_logs(vm):
    a = vm.src / "alpha"
    vm.container("boxen.alpha.db", "boxen-alpha", logs="ok\x1b[31mred\x1b[0m\x07\ttab\nnext\x00\x7f\x08\x0c\n")
    r = vm.ok("env", "logs", "db", cwd=a)
    assert r.stdout == "ok[31mred[0m\ttab\nnext\n"
    assert ["logs", "--tail", "100", "boxen.alpha.db"] in vm.docker_calls()
    vm.ok("env", "logs", "db", "--tail", "5", cwd=a)
    vm.ok("env", "logs", "db", "--tail=7", cwd=a)
    vm.ok("env", "logs", "env", cwd=a)                       # the build container itself
    calls = [c for c in vm.docker_calls() if c[0] == "logs"]
    assert calls[1:] == [["logs", "--tail", "5", "boxen.alpha.db"], ["logs", "--tail", "7", "boxen.alpha.db"],
                         ["logs", "--tail", "100", "boxen.alpha.env"]]
    vm.ok("env", "logs", "db", "-f", "--tail", "3", cwd=a)        # follow
    assert ["logs", "-f", "--tail", "3", "boxen.alpha.db"] in vm.docker_calls()
    vm.clear_logs()
    r = vm.boxen("env", "logs", "web", cwd=a)
    assert r.returncode == 1 and "no sidecar called 'web' (sidecars: db)" in r.stderr
    for bad in (["--tail", "0"], ["--tail", "x"], ["--tail", "5001"], ["--tail"], ["--tail="], ["-x"]):
        r = vm.boxen("env", "logs", "db", *bad, cwd=a)
        assert r.returncode == 1 and r.stderr.startswith("boxen: "), bad
    r = vm.boxen("env", "logs", cwd=a)
    assert r.returncode == 1 and "usage: boxen env logs <sidecar|env>" in r.stderr
    r = vm.boxen("env", "logs", "db", cwd=vm.src / "beta")       # beta has no sidecars
    assert r.returncode == 1 and "no sidecar called 'db' (sidecars: )" in r.stderr
    assert not any(c[0] == "logs" for c in vm.docker_calls())


def test_env_apply_waits_for_the_lock(vm):
    f = hold_lock(vm.pdir("alpha") / ".lock")
    try:
        p = subprocess.Popen([BOXEN, "env", "apply"], env=vm.env, cwd=vm.src / "alpha",
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(1)
        assert p.poll() is None and vm.compose_calls() == []
    finally:
        f.close()
    out, err = p.communicate(timeout=30)
    assert p.returncode == 0 and "environment applied" in out, err
    assert wait_for(lambda: len(vm.compose_calls()) == 2)
