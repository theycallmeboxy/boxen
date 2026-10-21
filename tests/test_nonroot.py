"""`boxen nonroot` and `boxen run --user`: non-root runs whose files are
still yours (a user namespace in the build container, allowed by a seccomp
profile only in projects that turn it on)."""
import json
import os

from test_run_env import EXEC_PREFIX, execs, supervised

MASK = 0xFFFFFFFF & ~0x10000000          # every unshare flag but CLONE_NEWUSER


def nonroot_file(vm, n="alpha"):
    return vm.pdir(n) / "compose.nonroot.yaml"


def test_nonroot_on_off(vm):
    a = vm.src / "alpha"
    assert vm.ok("nonroot", "alpha").stdout == "alpha: non-root runs off (boxen nonroot alpha on turns them on)\n"
    r = vm.ok("nonroot", "alpha", "on")
    assert r.stdout.endswith("alpha: non-root runs on: its agent can use boxen run --user\n")
    prof = vm.bhome / "seccomp-userns.json"
    f = nonroot_file(vm).read_text()
    assert "      - seccomp=%s\n" % prof in f and 'BOXEN_NONROOT: "1"' in f
    # Docker's default profile, plus one rule: unshare of a user namespace alone
    default = json.load(open(os.path.join(os.path.dirname(__file__), "..", "image", "seccomp",
                                          "docker-default.json")))
    got = json.loads(prof.read_text())
    assert got["syscalls"][:-1] == default["syscalls"] and got["defaultAction"] == default["defaultAction"]
    assert got["syscalls"][-1]["names"] == ["unshare"] and got["syscalls"][-1]["action"] == "SCMP_ACT_ALLOW"
    assert got["syscalls"][-1]["args"] == [{"index": 0, "value": MASK, "valueTwo": 0, "op": "SCMP_CMP_MASKED_EQ"}]
    # the running build container was recreated with it, the file among compose's
    calls = vm.compose_calls()
    assert calls and calls[-1]["args"][:2] == ["up", "-d"]
    assert str(nonroot_file(vm)) in calls[-1]["files"]
    assert vm.ok("nonroot", "alpha").stdout == "alpha: non-root runs on (boxen run --user)\n"
    assert "Non-root runs: on" in vm.ok("env", "show", cwd=a).stdout
    vm.clear_logs()
    assert vm.ok("nonroot", "alpha", "off").stdout.endswith("alpha: non-root runs off\n")
    assert not nonroot_file(vm).exists()
    assert str(nonroot_file(vm)) not in vm.compose_calls()[-1]["files"]
    assert "Non-root runs: off" in vm.ok("env", "show", cwd=a).stdout


def test_nonroot_never_started_project(vm):
    # beta has no container: nothing to recreate, nothing started
    vm.ok("nonroot", "beta", "on")
    assert nonroot_file(vm, "beta").exists()
    assert vm.compose_calls() == []


def test_nonroot_usage(vm):
    for args in (["nonroot"], ["nonroot", "alpha", "maybe"], ["nonroot", "alpha", "on", "x"], ["nonroot", "nope"]):
        assert vm.boxen(*args).returncode == 1, args
    assert "usage: boxen nonroot <name> [on|off]" in vm.boxen("nonroot", "alpha", "maybe").stderr


def test_run_user_needs_nonroot(vm):
    a = vm.src / "alpha"
    r = vm.boxen("run", "--user", "make", cwd=a)
    assert r.returncode == 1
    assert "non-root runs aren't on for alpha. The user turns them on with: boxen nonroot alpha on" in r.stderr
    assert execs(vm) == []


def test_run_user(vm):
    a = vm.src / "alpha"
    vm.ok("nonroot", "alpha", "on")
    vm.clear_logs()
    vm.ok("run", "--user", "./scripts/build", "linux", cwd=a)
    (a / "sub").mkdir()
    vm.ok("run", "-u", "-C", "sub", "make", cwd=a)
    vm.ok("run", "-d", "build", "--user", "make", "-j8", cwd=a)
    vm.ok("run", "--user", "-d", "b2", "make", cwd=a)
    (first, second, bg1, bg2) = execs(vm)
    argv, _ = supervised(first)
    assert argv[-(len(EXEC_PREFIX) + 3):] == EXEC_PREFIX + ["boxen-user", "./scripts/build", "linux"]
    argv, _ = supervised(second)
    assert argv[3] == str(a / "sub") and argv[-2:] == ["boxen-user", "make"]
    assert bg1[bg1.index("bash"):bg1.index("boxen-run")] == ["bash", "-lc", 'exec boxen-bg start "$@"']
    assert bg1[bg1.index("boxen-run") + 1:] == ["build", "boxen-user", "make", "-j8"]
    assert bg2[bg2.index("boxen-run") + 1:] == ["b2", "boxen-user", "make"]
    # --user goes with a command, not with --logs/--stop/--ps
    r = vm.boxen("run", "--user", "--ps", cwd=a)
    assert r.returncode == 1 and "--user goes with a command" in r.stderr


def test_setup_refreshes_nonroot(vm):
    vm.ok("nonroot", "alpha", "on")
    nonroot_file(vm).write_text("stale\n")
    (vm.bhome / "seccomp-userns.json").unlink()
    vm.ok("setup")
    assert "seccomp=" in nonroot_file(vm).read_text()
    assert (vm.bhome / "seccomp-userns.json").exists()
    assert not nonroot_file(vm, "beta").exists()


def test_busy_container_is_not_recreated(vm):
    """With something running in the build container (here: exec-local makes
    `docker exec ... ps` list this machine's processes), nonroot refuses and
    an update builds the new image but leaves the container for later."""
    (vm.fake / "exec-local").write_text("")
    r = vm.boxen("nonroot", "alpha", "on")
    assert r.returncode == 1 and "not now, turning it on recreates the build container" in r.stderr
    assert not nonroot_file(vm).exists() and vm.compose_calls() == []
    r = vm.fn("update_one alpha && echo rc=0 || echo rc=$?")
    assert "rc=76" in r.stdout and "alpha: new image built; not switched, something runs in it" in r.stderr
    assert [c["args"] for c in vm.compose_calls()] == [["build", "env"]]        # built, not recreated
    assert (vm.pdir("alpha") / "rebuild").exists()
    assert "Rebuild pending: " in vm.ok("env", "show", cwd=vm.src / "alpha").stdout
    # idle (the fake's exec prints nothing): rebuilt at once, mark cleared
    (vm.fake / "exec-local").unlink()
    vm.clear_logs()
    r = vm.fn("update_one alpha && echo rc=0 || echo rc=$?")
    assert "rc=0" in r.stdout
    assert [c["args"][0] for c in vm.compose_calls()] == ["build", "up"]
    assert not (vm.pdir("alpha") / "rebuild").exists()


def test_rebuild_pending(vm):
    """The timer's check: busy, nothing; idle once, it notes when; idle again
    REBUILD_IDLE_SECS later, it recreates the container on the built image."""
    p = vm.pdir("alpha")
    (p / "rebuild").write_text("1\n")
    (vm.fake / "exec-local").write_text("")
    vm.ok("rebuild-pending")
    assert vm.compose_calls() == [] and not (p / "rebuild.idle").exists()
    (vm.fake / "exec-local").unlink()
    vm.ok("rebuild-pending")
    assert vm.compose_calls() == [] and (p / "rebuild.idle").exists()
    vm.ok("rebuild-pending")                              # idle, but not for long enough yet
    assert vm.compose_calls() == []
    (p / "rebuild.idle").write_text("1000\n")             # long ago
    vm.load_docker(); vm.state["images"].append("boxen-env-alpha:latest"); vm.save_docker()
    r = vm.ok("rebuild-pending")
    assert "alpha: switched to its new build image" in r.stdout
    assert [c["args"] for c in vm.compose_calls()] == [["up", "-d", "--remove-orphans"]]   # no build: it's built
    assert not (p / "rebuild").exists() and not (p / "rebuild.idle").exists()
    # busy again between the two checks: starts over
    (p / "rebuild").write_text("1\n")
    (p / "rebuild.idle").write_text("1000\n")
    (vm.fake / "exec-local").write_text("")
    vm.clear_logs()
    vm.ok("rebuild-pending")
    assert vm.compose_calls() == [] and not (p / "rebuild.idle").exists() and (p / "rebuild").exists()
    # a stopped container just drops the mark (its next start uses the new image)
    (vm.fake / "exec-local").unlink()
    vm.container("boxen.alpha.env", "boxen-alpha", state="exited", status="Exited (0)")
    vm.ok("rebuild-pending")
    assert not (p / "rebuild").exists() and vm.compose_calls() == []


def test_rebuild_waits_for_runs(vm):
    """A boxen run in progress (its run lock) or one started recently keeps
    the swap off; Docker unreachable keeps the marks; a failed swap waits."""
    import fcntl
    import time
    p = vm.pdir("alpha")
    (p / "rebuild").write_text("1\n")
    (p / "rebuild.idle").write_text("1000\n")
    vm.load_docker(); vm.state["images"].append("boxen-env-alpha:latest"); vm.save_docker()
    # a run holding the lock (shared, as boxen run takes it)
    with open(p / ".run.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        vm.ok("rebuild-pending")
    assert vm.compose_calls() == [] and (p / "rebuild").exists()
    # one started a minute ago
    (p / "rebuild.idle").write_text("1000\n")
    (p / ".last-run").write_text("")
    os.utime(p / ".last-run", (time.time() - 60,) * 2)
    vm.ok("rebuild-pending")
    assert vm.compose_calls() == [] and not (p / "rebuild.idle").exists()
    # boxen run stamps it
    vm.ok("run", "true", cwd=vm.src / "alpha")
    assert time.time() - (p / ".last-run").stat().st_mtime < 30
    # Docker down: nothing touched
    os.utime(p / ".last-run", (time.time() - 3600,) * 2)
    (p / "rebuild.idle").write_text("1000\n")
    vm.set_docker(down=True)
    vm.ok("rebuild-pending")
    assert (p / "rebuild").exists() and (p / "rebuild.idle").exists()
    vm.set_docker(down=False)
    # a failed swap: logged, retried only after an hour
    vm.set_docker(fail=["up"])
    vm.clear_logs()
    r = vm.ok("rebuild-pending")
    assert "switching to its new build image failed (next try in an hour" in r.stdout
    assert (p / "rebuild.failed").exists() and (p / "rebuild").exists()
    vm.set_docker(fail=[])
    (p / "rebuild.idle").write_text("1000\n")
    vm.clear_logs()
    vm.ok("rebuild-pending")
    assert vm.compose_calls() == []
    (p / "rebuild.failed").write_text("1000\n")
    vm.ok("rebuild-pending")
    assert [c["args"][0] for c in vm.compose_calls()] == ["up"]
    assert not (p / "rebuild").exists()


def test_env_busy_fails_closed(vm):
    """docker exec failing (pids limit, no ps) counts as busy."""
    from conftest import write_exe
    real = vm.bin / "docker"
    os.rename(real, vm.bin / "docker.real")
    write_exe(str(real), '#!/bin/sh\n[ "$1" = exec ] && exit 1\nexec "%s" "$@"\n' % (vm.bin / "docker.real"))
    r = vm.fn("env_busy alpha && echo BUSY || echo IDLE")
    assert "BUSY" in r.stdout and "couldn't look inside it" in r.stdout


def test_nonroot_on_rebuilds_an_old_image(vm):
    """A project image from before boxen-user (the fake's `docker run` of
    test -x fails) is rebuilt when non-root runs are turned on."""
    vm.load_docker(); vm.state["images"].append("boxen-env-beta:latest"); vm.save_docker()
    from conftest import write_exe
    real = vm.bin / "docker"
    os.rename(real, vm.bin / "docker.real")
    write_exe(str(real), '#!/bin/sh\nfor a; do [ "$a" = /usr/local/bin/boxen-user ] && exit 1; done\n'
                         'exec "%s" "$@"\n' % (vm.bin / "docker.real"))
    vm.ok("nonroot", "beta", "on")
    assert [c["args"] for c in vm.compose_calls()] == [["build", "env"]]
