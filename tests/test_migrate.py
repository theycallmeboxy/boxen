"""lib/migrate.sh: moving a boxen v1 install (agents in containers on the
system Docker daemon) to v2, with a fake v1 daemon behind `docker -H`."""
import json
import os
import re
import subprocess

import pytest

from conftest import BOXEN, FORMAT_MARK, REPO, unix_socket

MIGRATE = os.path.join(REPO, "lib", "migrate.sh")


def deep_merge(a, b):
    prog = subprocess.run(["bash", "-c", 'source "$1"; printf %s "$DEEP_MERGE"', "x", MIGRATE],
                          capture_output=True, text=True, check=True).stdout
    r = subprocess.run(["jq", "-cs", prog], input=json.dumps(a) + json.dumps(b), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_deep_merge():
    v1 = {"a": 1, "s": "v1", "o": {"x": 1, "l": [1, 2], "deep": {"k": "v1"}}, "l": [1, {"k": 1}],
          "t": {"obj": True}, "only_v1": [3]}
    vm = {"s": "vm", "o": {"y": 2, "l": [2, 3], "deep": {"k": "vm", "j": 0}}, "l": [{"k": 1}, 3], "t": [1],
          "n": None, "f": False}
    assert deep_merge(v1, vm) == {
        "a": 1, "s": "vm", "o": {"x": 1, "y": 2, "l": [1, 2, 3], "deep": {"k": "vm", "j": 0}},
        "l": [1, {"k": 1}, 3], "t": [1], "only_v1": [3], "n": None, "f": False}
    assert deep_merge({}, {}) == {}
    assert deep_merge({"permissions": {"allow": ["Bash(npm *)"]}},
                      {"permissions": {"allow": ["Bash(boxen run *)", "Bash(npm *)"]}}) == \
        {"permissions": {"allow": ["Bash(npm *)", "Bash(boxen run *)"]}}


def v1_dockerfile(text):
    r = subprocess.run(["bash", "-c", 'source "$1"; v1_dockerfile', "x", MIGRATE], input=text,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_v1_dockerfile():
    out = v1_dockerfile("FROM boxen-base:latest\nUSER root\nRUN apt-get install -y golang\n"
                        "  user claude\nRUN echo USER x\nENV USERNAME=x\nUSER\tclaude\n")
    lines = out.splitlines()
    assert lines[0].startswith("# Moved from boxen v1 by 'boxen migrate'")
    assert lines[2:] == ["FROM boxen-env:latest", "RUN apt-get install -y golang", "RUN echo USER x",
                         "ENV USERNAME=x"]
    for src, want in (("FROM boxen-base\n", "FROM boxen-env:latest"),
                      ("from boxen-base:core AS dev\n", "from boxen-env:latest AS dev"),
                      ("  FROM   boxen-base:latest\n", "  FROM   boxen-env:latest"),
                      ("FROM ubuntu:24.04\n", "FROM ubuntu:24.04"),
                      ("FROM boxen-env:latest\n", "FROM boxen-env:latest")):
        assert v1_dockerfile(src).splitlines()[2] == want, src


# ------------------------------------------------------------ a full run

PG_YAML = "services:\n  postgres:\n    image: postgres:16\n    volumes: [pgdata:/var/lib/postgresql/data]\n"
PG_CONFIG = {"services": {"postgres": {"image": "postgres:16", "networks": {"default": None},
                                       "volumes": [{"type": "volume", "source": "pgdata",
                                                    "target": "/var/lib/postgresql/data", "volume": {}}]}}}


@pytest.fixture
def v1(vm):
    """vm plus a v1 install: projects old (env/, sidecar data on the system
    daemon, a GitHub token) and supervisor, and v1's claude-config volume."""
    p = vm.pdir("old")
    (p / "compose.yaml").write_text("name: boxen-old\nservices:\n  claude: {}\n")
    (p / "env").mkdir()
    (p / "env" / "Dockerfile").write_text("FROM boxen-base:latest\nUSER root\nRUN apt-get install -y golang\n"
                                          "USER claude\n")
    (p / "env" / "services.yaml").write_text(PG_YAML)
    (p / "compose.override.yaml").write_text("services:\n  claude:\n    ports: ['3000:3000']\n")
    (p / "agent.log").write_text("log\n")
    (p / "project.env").write_text("X=1\n")
    (p / "repos").write_text("theycallmeboxy/old\n")
    (p / "token").mkdir()
    (p / "token" / "gh-token").write_text("TOKEN\n")
    (vm.src / "old" / "main.go").write_text("package main\n")
    sup = vm.pdir("supervisor")
    sup.mkdir()
    (sup / "compose.yaml").write_text("name: boxen-supervisor\n")
    vm.set_docker(compose_config=PG_CONFIG, v1={
        "images": ["boxen-base:latest"],
        "containers": {"boxen-old": {"running": True, "image": "sha256:proj-old"},
                       "boxen.old.postgres": {"running": True, "image": "sha256:postgres"},
                       "boxen-supervisor": {"running": False, "image": "sha256:proj-sup"},
                       "other": {"running": True, "image": "sha256:other"}},
        "images_q": ["sha256:base", "sha256:proj-old"],
        "volumes": {
            "claude-config": {
                "settings.json": json.dumps({
                    "model": "opus", "theme": "v1", "env": {"A": "1"},
                    "permissions": {"allow": ["Bash(*)", "Edit"], "defaultMode": "bypassPermissions"},
                    "sandbox": {"enabled": False}, "skipDangerousModePermissionPrompt": True}),
                ".claude.json": json.dumps({"oauthAccount": "v1", "projects": {"/home/claude/old": {"t": 1}}}),
                "CLAUDE.md": "# v1 notes\n",
                "boxen.md": "v1's boxen.md\n",
                "skills/carry/SKILL.md": "v1 carry\n",
                "skills/mine/SKILL.md": "mine\n",
                "projects/-home-claude-old/s1.jsonl": "{}\n",
            },
            "boxen.old.pgdata": {"PG_VERSION": "16\n"},
            "boxen-home-old": {".bashrc": "x\n"},
        }})
    # the VM's own Claude config, from install.sh
    c = vm.home / ".claude"
    c.mkdir()
    (c / "settings.json").write_text(json.dumps({"theme": "vm", "permissions": {"allow": ["Bash(boxen run *)"]}}))
    (c / "CLAUDE.md").write_text("@boxen.md\n")
    vm.sock = unix_socket(vm.tmp / "system-docker.sock")
    yield vm
    vm.sock.close()


def migrate_script(vm, args):
    return 'source "$BOXEN_REPO/lib/migrate.sh"\nV1_SOCK=%s\nmigrate_main %s\n' % (
        vm.tmp / "system-docker.sock", " ".join(args))


def migrate(vm, *args):
    """boxen migrate (--yes unless args are given) with no terminal."""
    return vm.fn(migrate_script(vm, args or ("--yes",)))


def v1_calls(vm):
    return [c["argv"][2:] for c in vm.docker_log() if c.get("v1")]


def test_migrate(v1):
    vm = v1
    r = migrate(vm)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "==> Moved. Next:" in r.stdout
    p = vm.pdir("old")
    # v1's files are kept in v1/, a v2 compose.yaml replaces them
    assert sorted(os.listdir(p / "v1")) == ["agent.log", "compose.override.yaml", "compose.yaml", "env",
                                            "project.env"]
    assert (p / "v1" / "env" / "Dockerfile").exists()
    assert "your v1 compose.override.yaml is in %s/v1/" % p in r.stdout
    c = (p / "compose.yaml").read_text()
    assert c.splitlines()[0] == FORMAT_MARK and "container_name: boxen.old.env" in c
    assert not (p / "compose.override.yaml").exists()
    # .boxen/ from env/
    b = vm.src / "old" / ".boxen"
    df = (b / "Dockerfile").read_text()
    assert df.endswith("container).\nFROM boxen-env:latest\nRUN apt-get install -y golang\n")
    assert (b / "services.yaml").read_text() == PG_YAML
    assert (vm.src / "old" / "main.go").exists()
    # GitHub state stays
    assert (p / "repos").read_text() == "theycallmeboxy/old\n"
    assert (p / "token" / "gh-token").read_text() == "TOKEN\n"
    # the supervisor is set aside
    assert not vm.pdir("supervisor").exists()
    assert (vm.projects / ".v1-supervisor" / "compose.yaml").exists()
    r2 = vm.fn("projects; echo --; v1_projects")
    assert r2.stdout == "alpha\nbeta\nold\n--\n"
    # the sidecar volume was copied (and only it: no home volume)
    log = vm.docker_log()
    v1calls = [c for c in log if c.get("v1")]
    assert v1calls and all(c["argv"][:2] == ["-H", "unix://%s" % (vm.tmp / "system-docker.sock")] for c in v1calls)
    assert ["volume", "create", "--label", "boxen.project=old", "boxen.old.pgdata"] in [c["argv"] for c in log]
    (restore,) = [c for c in log if not c.get("v1") and c["argv"][0] == "run" and "-i" in c["argv"]]
    assert "boxen.old.pgdata:/v" in restore["argv"] and restore["stdin_members"] == ["./PG_VERSION"]
    assert not any("boxen-home-old" in json.dumps(c["argv"]) for c in log)
    # v1's containers stopped, then the build container built and started
    # exactly v1's running containers were stopped (one call), before anything moved
    stops = [c for c in v1_calls(vm) if c[0] == "stop"]
    assert stops == [["stop", "boxen-old", "boxen.old.postgres"]]
    assert vm.load_docker()["v1"]["containers"]["other"]["running"] is True
    assert [(x["project"], x["args"][0]) for x in vm.compose_calls()] == [("old", "build"), ("old", "up")]
    assert json.loads((p / "compose.services.yaml").read_text())["services"]["postgres"]["container_name"] == \
        "boxen.old.postgres"
    # Claude's config came across
    c = vm.home / ".claude"
    s = json.loads((c / "settings.json").read_text())
    # v1's keys, the VM's values win; v1's permission and sandbox settings (for
    # a container) don't come across; boxen's two defaults on top
    assert s == {"model": "opus", "theme": "vm", "env": {"A": "1"},
                 "permissions": {"allow": ["Bash(boxen run *)"]},
                 "cleanupPeriodDays": 365, "remoteControlAtStartup": True}
    assert "v1's permission and sandbox settings were for a container and aren't carried over" in r.stdout
    v1s = json.loads((c / "settings.json.v1").read_text())
    assert v1s["theme"] == "v1" and v1s["permissions"]["defaultMode"] == "bypassPermissions"   # all of it
    assert json.loads((vm.home / ".claude.json").read_text())["oauthAccount"] == "v1"
    assert (c / "CLAUDE.md").read_text() == "# v1 notes\n\n@boxen.md\n"
    assert (c / "boxen.md").read_text() == open(os.path.join(REPO, "templates", "boxen.md")).read()
    assert (c / "skills" / "carry" / "SKILL.md").read_text() != "v1 carry\n"
    assert (c / "skills" / "mine" / "SKILL.md").read_text() == "mine\n"
    assert (c / "projects" / "-home-claude-old" / "s1.jsonl").exists()
    assert (vm.bhome / ".migrated-claude").exists()
    # again: nothing left to do
    vm.clear_logs()
    r = migrate(vm)
    assert r.returncode == 0, r.stderr
    assert "Claude config: moved already" in r.stdout
    assert vm.compose_calls() == []


def test_migrate_settings_without_vm_settings(v1):
    """No settings.json on the VM yet: v1's, minus its permissions."""
    (v1.home / ".claude" / "settings.json").unlink()
    r = migrate(v1)
    assert r.returncode == 0, r.stderr
    s = json.loads((v1.home / ".claude" / "settings.json").read_text())
    assert s == {"model": "opus", "theme": "v1", "env": {"A": "1"}, "cleanupPeriodDays": 365,
                 "remoteControlAtStartup": True}


def test_migrate_settings_plain_v1(v1):
    """v1 settings with nothing to drop: merged, no notice."""
    st = v1.load_docker()
    st["v1"]["volumes"]["claude-config"]["settings.json"] = json.dumps({"model": "opus", "permissions": {}})
    v1.save_docker()
    r = migrate(v1)
    assert r.returncode == 0, r.stderr
    assert "aren't carried over" not in r.stdout
    s = json.loads((v1.home / ".claude" / "settings.json").read_text())
    assert s["model"] == "opus" and s["permissions"] == {"allow": ["Bash(boxen run *)"]}


def test_migrate_keeps_an_existing_boxen_dir_and_vm_claude_md(v1):
    vm = v1
    b = vm.src / "old" / ".boxen"
    b.mkdir()
    (b / "Dockerfile").write_text("FROM boxen-env:latest\n# mine\n")
    (vm.home / ".claude" / "CLAUDE.md").write_text("# VM notes\n@boxen.md\n")
    (vm.home / ".claude.json").write_text(json.dumps({"oauthAccount": "vm", "projects": {"/x": {"t": 2}}}))
    r = migrate(vm)
    assert r.returncode == 0, r.stderr
    assert "exists already; left as it is" in r.stdout
    assert (b / "Dockerfile").read_text() == "FROM boxen-env:latest\n# mine\n"
    c = vm.home / ".claude"
    assert (c / "CLAUDE.md").read_text() == "# VM notes\n@boxen.md\n"
    assert (c / "CLAUDE.md.v1").read_text() == "# v1 notes\n"
    cj = json.loads((vm.home / ".claude.json").read_text())
    assert cj["oauthAccount"] == "vm" and set(cj["projects"]) == {"/x", "/home/claude/old"}
    assert (vm.home / ".claude.json.before-migrate").exists()


def test_migrate_project_without_code(v1):
    import shutil
    shutil.rmtree(v1.src / "old")
    r = migrate(v1)
    assert r.returncode == 0
    assert "old: its code folder %s isn't there; put it back and run boxen migrate again, or drop the project " \
        "with: rm -rf %s" % (v1.src / "old", v1.pdir("old")) in r.stderr
    assert "not moved (still v1; boxen migrate again tries them again): old" in r.stderr
    assert (v1.pdir("old") / "compose.yaml").read_text().startswith("name: boxen-old")     # untouched


def test_migrate_needs_the_system_daemon(vm):
    r = vm.fn('source "$BOXEN_REPO/lib/migrate.sh"\nV1_SOCK=%s\nmigrate_main\n' % (vm.tmp / "nosock"))
    assert r.returncode == 1 and "can't reach the system Docker daemon, where v1 lives" in r.stderr
    for args in (["--yes", "--cleanup"], ["--yes", "x"], ["now"]):
        r = vm.boxen("migrate", *args)
        assert r.returncode == 1 and "usage: boxen migrate [--cleanup | --yes]" in r.stderr, args
    r = vm.boxen("migrate", "now")
    assert r.returncode == 1 and "usage: boxen migrate [--cleanup | --yes]" in r.stderr
    r = vm.boxen("migrate", "--cleanup")
    assert r.returncode == 1 and "these v1 projects haven't moved yet: old" in r.stderr


def test_migrate_asks_before_stopping_v1(v1):
    vm = v1
    # no terminal, no --yes, containers running: refused, nothing touched
    r = vm.fn(migrate_script(vm, ()))
    assert r.returncode == 1
    assert "migrate stops v1's containers first; run it in a terminal, or add --yes" in r.stderr
    assert not any(c[0] == "stop" for c in v1_calls(vm))
    assert vm.fn("v1_projects").stdout == "old\nsupervisor\n"
    # in a terminal: lists them and asks; no
    r = vm.fn_tty(migrate_script(vm, ()), input="n\n")
    assert r.returncode == 0, r.stdout
    assert "This stops v1's running containers (any Claude session in them ends):" in r.stdout
    assert "    boxen-old\n    boxen.old.postgres\nGo ahead? [y/N]" in r.stdout and "boxen-supervisor" not in r.stdout
    assert "aborted" in r.stdout
    assert not any(c[0] == "stop" for c in v1_calls(vm)) and vm.fn("v1_projects").stdout == "old\nsupervisor\n"
    # yes
    r = vm.fn_tty(migrate_script(vm, ()), input="y\n")
    assert r.returncode == 0 and "==> Moved." in r.stdout, r.stdout
    assert [c for c in v1_calls(vm) if c[0] == "stop"] == [["stop", "boxen-old", "boxen.old.postgres"]]
    assert vm.fn("v1_projects").stdout == ""


def test_migrate_without_running_containers_doesnt_ask(v1):
    st = v1.load_docker()
    for c in st["v1"]["containers"].values():
        c["running"] = False
    v1.save_docker()
    r = v1.fn(migrate_script(v1, ()))
    assert r.returncode == 0, r.stderr
    assert not any(c[0] == "stop" for c in v1_calls(v1))


def test_migrate_through_sudo(v1):
    """Out of the docker group already: the system daemon through sudo."""
    st = v1.load_docker()
    st["v1"]["needs_sudo"] = True
    v1.save_docker()
    r = migrate(v1)
    assert r.returncode == 0, r.stderr
    v1log = [c for c in v1.docker_log() if c.get("v1")]
    ok = [c for c in v1log if c["sudo"]]
    assert ok and all(c["sudo"] for c in v1log[1:])          # the first try, without, was refused
    sudo = (v1.fake / "sudo.log").read_text().splitlines()
    assert sudo[0] == "docker -H unix://%s info" % (v1.tmp / "system-docker.sock")
    assert (v1.pdir("old") / "compose.yaml").read_text().startswith(FORMAT_MARK)
    assert ["volume", "create", "--label", "boxen.project=old", "boxen.old.pgdata"] in v1.docker_calls()


def test_migrate_failed_volume_copy_changes_nothing(v1):
    vm = v1
    p = vm.pdir("old")
    before = sorted(os.listdir(p) + [".lock"])
    vm.set_docker(fail_restore=["boxen.old.pgdata"])
    r = migrate(vm)
    assert r.returncode == 0
    assert "old: copying volume boxen.old.pgdata failed; nothing changed for old (v1's data is untouched). Run " \
           "boxen migrate again." in r.stderr
    assert "not moved (still v1; boxen migrate again tries them again): old" in r.stderr
    # still v1: nothing moved, no .boxen, the half-made volume removed
    assert sorted(os.listdir(p)) == before
    assert not (p / "compose.yaml").read_text().startswith(FORMAT_MARK)
    assert not (vm.src / "old" / ".boxen").exists()
    assert "boxen.old.pgdata" not in vm.load_docker()["volumes"]
    assert ["volume", "rm", "boxen.old.pgdata"] in vm.docker_calls()
    assert vm.compose_calls() == []
    assert vm.fn("v1_projects").stdout == "old\n"
    # again, working this time
    vm.set_docker(fail_restore=[])
    r = migrate(vm)
    assert r.returncode == 0 and "these need a look" not in r.stderr, r.stderr
    assert (p / "compose.yaml").read_text().startswith(FORMAT_MARK)


def test_migrate_copies_env_folder(v1):
    vm = v1
    env = vm.pdir("old") / "env"
    (env / "requirements.txt").write_text("flask\n")
    (env / ".npmrc").write_text("x=1\n")
    (env / "conf" / "sub").mkdir(parents=True)
    (env / "conf" / "sub" / "a.conf").write_text("a\n")
    (env / "STATUS.md").write_text("v1 status\n")
    os.symlink("/etc/passwd", env / "passwd")
    os.symlink(env / "conf", env / "conflink")
    (vm.pdir("old") / "project.env").write_text("# mine\nFOO=1\n  BAR_2=x y\nnot a var\n")
    r = migrate(vm)
    assert r.returncode == 0, r.stderr
    b = vm.src / "old" / ".boxen"
    assert sorted(os.listdir(b)) == [".npmrc", "Dockerfile", "conf", "requirements.txt", "services.yaml"]
    assert (b / "requirements.txt").read_text() == "flask\n"
    assert (b / "conf" / "sub" / "a.conf").read_text() == "a\n"
    assert "FROM boxen-env:latest" in (b / "Dockerfile").read_text()
    assert "    project.env (FOO BAR_2) isn't used by v2: the agent runs on the VM now." in r.stdout
    assert (vm.pdir("old") / "v1" / "project.env").exists()
    assert (vm.pdir("old") / "v1" / "env" / "STATUS.md").exists()


def test_migrate_shared(v1):
    """v1's boxen.env commit identity and base.local.Dockerfile."""
    vm = v1
    (vm.bhome / "boxen.env").write_text("TZ=UTC\nGIT_AUTHOR_NAME=Boxy B\nGIT_AUTHOR_EMAIL=1+boxy@users.noreply.github.com\n")
    (vm.bhome / "base.local.Dockerfile").write_text("FROM boxen-base:core\nUSER root\nRUN apt-get install -y vim\n"
                                                    "USER claude\n")
    r = migrate(vm)
    assert r.returncode == 0, r.stderr
    gc = str(vm.bhome / "gitconfig")
    get = lambda k: subprocess.run(["git", "config", "-f", gc, k], capture_output=True, text=True).stdout  # noqa
    assert get("user.name") == "Boxy B\n" and get("user.email") == "1+boxy@users.noreply.github.com\n"
    assert "commit identity from v1: Boxy B <1+boxy@users.noreply.github.com>" in r.stdout
    assert (vm.bhome / "env.local.Dockerfile").read_text() == "FROM boxen-env:core\nRUN apt-get install -y vim\n"
    assert ["build", "-t", "boxen-env:latest", "-"] in vm.docker_calls()
    assert "base.local.Dockerfile (v1's additions for every project) is now" in r.stdout


def test_migrate_shared_keeps_whats_set(v1):
    vm = v1
    vm.fn("write_gitconfig Me me@example.com")
    (vm.bhome / "boxen.env").write_text("GIT_AUTHOR_NAME=Boxy\nGIT_AUTHOR_EMAIL=b@x\n")
    (vm.bhome / "base.local.Dockerfile").write_text("FROM boxen-base:core\n")
    (vm.bhome / "env.local.Dockerfile").write_text("FROM boxen-env:core\n# mine\n")
    r = migrate(vm)
    assert r.returncode == 0, r.stderr
    assert vm.fn("git_ident email").stdout == "me@example.com\n"
    assert (vm.bhome / "env.local.Dockerfile").read_text() == "FROM boxen-env:core\n# mine\n"
    assert "commit identity from v1" not in r.stdout
    # no email in boxen.env: nothing
    assert not any(c[:2] == ["build", "-t"] and c[-1] == "-" for c in vm.docker_calls())


def test_migrate_claude_json_not_signed_in_on_the_vm(v1):
    """The VM has a .claude.json but no login: v1's file, with the VM's on top."""
    (v1.home / ".claude.json").write_text(json.dumps({"theme": "vm", "projects": {"/x": {"t": 2}},
                                                      "tips": {"a": 1}}))
    st = v1.load_docker()
    st["v1"]["volumes"]["claude-config"][".claude.json"] = json.dumps(
        {"oauthAccount": {"email": "v1@x"}, "hasCompletedOnboarding": True, "theme": "v1", "tips": {"b": 2},
         "projects": {"/home/claude/old": {"t": 1}, "/x": {"t": 0}}})
    v1.save_docker()
    r = migrate(v1)
    assert r.returncode == 0, r.stderr
    cj = json.loads((v1.home / ".claude.json").read_text())
    assert cj == {"oauthAccount": {"email": "v1@x"}, "hasCompletedOnboarding": True, "theme": "vm",
                  "tips": {"a": 1, "b": 2}, "projects": {"/home/claude/old": {"t": 1}, "/x": {"t": 2}}}
    assert json.loads((v1.home / ".claude.json.before-migrate").read_text())["theme"] == "vm"


def cleanup(vm, answer="y\n"):
    return vm.fn_tty('source "$BOXEN_REPO/lib/migrate.sh"\nV1_SOCK=%s\nmigrate_main --cleanup\n'
                     % (vm.tmp / "system-docker.sock"), input=answer)


@pytest.fixture
def migrated(v1):
    """v1 after a successful migrate, with v1's other files around."""
    (v1.bhome / "boxen.env").write_text("GIT_AUTHOR_EMAIL=b@x\n")
    (v1.bhome / "base.local.Dockerfile").write_text("FROM boxen-base:core\n")
    (v1.bhome / "agent").mkdir()
    (v1.bhome / "agent" / "agent.sock").write_text("")
    (v1.bhome / "agent.log").write_text("x\n")
    (v1.src / "supervisor").mkdir()
    r = migrate(v1)
    assert r.returncode == 0, r.stderr
    assert (v1.pdir("old") / "v1").is_dir() and (v1.projects / ".v1-supervisor").is_dir()
    v1.clear_logs()
    return v1


def test_cleanup(migrated):
    vm = migrated
    r = cleanup(vm, "n\n")
    assert "aborted" in r.stdout and (vm.bhome / "boxen.env").exists()
    assert not [c for c in v1_calls(vm) if c[0] in ("rm", "volume") and c[1] in ("-f", "rm")]
    r = cleanup(vm)
    assert r.returncode == 0, r.stdout
    out = r.stdout
    assert "3 container(s), 4 image(s) and the build cache" in out
    calls = v1_calls(vm)
    (rm,) = [c for c in calls if c[:2] == ["rm", "-f"]]
    assert sorted(rm[2:]) == ["boxen-old", "boxen-supervisor", "boxen.old.postgres"]     # sidecars too, not "other"
    (vrm,) = [c for c in calls if c[:2] == ["volume", "rm"]]
    assert sorted(vrm[2:]) == ["boxen-home-old", "boxen.old.pgdata", "claude-config"]
    (irm,) = [c for c in calls if c[:2] == ["image", "rm"]]
    assert sorted(irm[3:]) == ["sha256:base", "sha256:postgres", "sha256:proj-old", "sha256:proj-sup"]
    assert ["builder", "prune", "-af"] in calls and ["network", "prune", "-f"] in calls
    for f in ("boxen.env", "base.local.Dockerfile", "agent", "agent.log"):
        assert not (vm.bhome / f).exists(), f
    assert not (vm.pdir("old") / "v1").exists() and not (vm.projects / ".v1-supervisor").exists()
    assert (vm.pdir("old") / "compose.yaml").exists() and (vm.pdir("old") / "token" / "gh-token").exists()
    assert "systemctl disable --now docker.service docker.socket containerd.service" in \
        (vm.fake / "sudo.log").read_text()
    assert "Kept, for you to look at or delete:" in out
    assert "    %s" % (vm.home / ".claude" / "settings.json.v1") in out
    assert "    %s" % (vm.src / "supervisor") in out
    assert ".claude.json.before-migrate" not in out                  # (there was none)


def test_cleanup_refuses_uncopied_sidecar_volume(migrated):
    vm = migrated
    st = vm.load_docker()
    st["v1"]["volumes"]["boxen.old.cache"] = {"x": "1"}
    vm.save_docker()
    r = cleanup(vm)
    assert r.returncode == 1
    assert "these v1 volumes were never copied to rootless Docker: boxen.old.cache" in r.stdout
    assert not [c for c in v1_calls(vm) if c[0] in ("rm", "image", "builder") or c[:2] == ["volume", "rm"]]
    assert (vm.bhome / "boxen.env").exists() and (vm.pdir("old") / "v1").exists()


def test_cleanup_without_the_daemon(migrated):
    vm = migrated
    vm.sock.close()
    os.unlink(vm.tmp / "system-docker.sock")
    r = cleanup(vm)
    assert r.returncode == 0, r.stdout
    assert "The system Docker daemon isn't reachable, so nothing on it is deleted" in r.stdout
    assert v1_calls(vm) == []
    assert not (vm.bhome / "boxen.env").exists() and not (vm.pdir("old") / "v1").exists()
    assert "systemctl disable --now docker.service" in (vm.fake / "sudo.log").read_text()


def test_cleanup_needs_a_terminal_and_everything_moved(vm):
    r = vm.boxen("migrate", "--cleanup")
    assert r.returncode == 1 and "these v1 projects haven't moved yet: old" in r.stderr
    import shutil
    shutil.rmtree(vm.pdir("old"))
    r = vm.boxen("migrate", "--cleanup")
    assert r.returncode == 1 and "--cleanup asks first; run it in a terminal" in r.stderr
    r = vm.boxen("migrate", "--cleanup", "x")
    assert r.returncode == 1 and "usage: boxen migrate" in r.stderr


def doctor(vm, managed, *args):
    """boxen doctor, with the managed settings file at MANAGED."""
    r = vm.fn('source "$BOXEN_REPO/lib/doctor.sh"\nMANAGED=%s\ndoctor_main %s || echo "rc=$?"\n'
              % (managed, " ".join(args)))
    r.stdout = re.sub("\x1b\\[[0-9;]*m", "", r.stdout)
    return r


def test_doctor_runs(vm):
    """doctor runs every check with fakes and reports (some fail here: the
    fake Docker writes no files, there's no lingering)."""
    vm.ok("setup")
    managed = vm.tmp / "managed.json"
    managed.write_text(vm.ok("managed-settings").stdout)
    r = doctor(vm, managed)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    for head in ("Rootless Docker (build containers)", "Claude Code and its sandbox (agents)", "GitHub", "Projects"):
        assert head in out
    assert "the daemon at %s is rootless" % vm.env["BOXEN_DOCKER_HOST"] in out
    assert "ok    managed settings: sandbox on and required" in out and str(managed) in out
    assert "takes more commands out of the sandbox" not in out and "allows without asking" not in out
    assert "agents get boxen's instructions" in out
    assert "git in %s uses boxen's tokens and identity" % vm.src in out
    assert "warn  no commit identity for %s" % vm.src in out
    assert 'Then /status there: "Setting sources" must list managed settings.' in out
    vm.fn("write_gitconfig 'Boxy B' b@example.com")
    assert "ok    commits in %s: Boxy B <b@example.com>" % vm.src in doctor(vm, managed).stdout
    assert "ok    in %s, git asks only boxen for GitHub credentials" % vm.src in out
    assert "boxen v1 projects to move: old" in out
    assert "2 v2 project(s)" in out
    assert not [f for f in os.listdir(vm.src) if f.startswith(".boxen-doctor")]      # its test repo is gone
    r = vm.boxen("doctor", "--frob")
    assert r.returncode == 1 and "usage: boxen doctor" in r.stderr


def test_doctor_managed_settings(vm):
    vm.ok("setup")
    managed = vm.tmp / "managed.json"
    r = doctor(vm, managed)
    assert "FAIL  boxen's sandbox settings aren't installed (%s missing): re-run install.sh" % managed in r.stdout
    assert "rc=1" in r.stdout
    # the same JSON, laid out differently: fine
    managed.write_text(json.dumps(json.loads(vm.ok("managed-settings").stdout)))
    assert "ok    managed settings" in doctor(vm, managed).stdout
    # changed (an older boxen's, or edited)
    m = json.loads(managed.read_text())
    m["sandbox"]["excludedCommands"].append("docker *")
    managed.write_text(json.dumps(m))
    r = doctor(vm, managed)
    assert "FAIL  %s differs from what this boxen writes: re-run install.sh" % managed in r.stdout
    managed.write_text("not json")
    assert "differs from what this boxen writes" in doctor(vm, managed).stdout


def test_doctor_warns_about_your_settings(vm):
    vm.ok("setup")
    managed = vm.tmp / "managed.json"
    managed.write_text(vm.ok("managed-settings").stdout)
    c = vm.home / ".claude"
    (c / "settings.json").write_text(json.dumps({
        "sandbox": {"excludedCommands": ["boxen run *", "docker *", "make *"]},
        "permissions": {"allow": ["Edit", "Write(/**)", "Bash(npm test)", "Read", "Edit(src/**)"],
                        "defaultMode": "bypassPermissions"}}))
    (c / "settings.local.json").write_text(json.dumps({"permissions": {"allow": ["Bash", "Bash(*)"]}}))
    out = doctor(vm, managed).stdout
    s, local = c / "settings.json", c / "settings.local.json"
    assert "warn  %s takes more commands out of the sandbox: docker *, make *" % s in out
    assert "warn  %s allows without asking: Edit, Write(/**)" % s in out
    assert "warn  %s asks for bypassPermissions mode by default" % s in out
    assert "warn  %s allows without asking: Bash, Bash(*)" % local in out
    assert "%s takes more" % local not in out


def test_doctor_credential_helpers(vm):
    """What git really asks in a repo under ~/src: after boxen's include, a
    reset and another helper (gh auth setup-git's) is a failure."""
    vm.ok("setup")
    managed = vm.tmp / "managed.json"
    managed.write_text(vm.ok("managed-settings").stdout)
    with open(vm.home / ".gitconfig", "a") as f:
        f.write('[credential "https://github.com"]\n\thelper =\n\thelper = !/usr/bin/gh auth git-credential\n')
    out = doctor(vm, managed).stdout
    assert ("FAIL  in %s, git asks other credential helpers too (!/usr/bin/gh auth git-credential): re-run "
            "install.sh" % vm.src) in out
    # a helper added without a reset: asked after boxen's
    (vm.home / ".gitconfig").write_text("")
    vm.ok("setup")
    with open(vm.home / ".gitconfig", "a") as f:
        f.write('[credential "https://github.com"]\n\thelper = store\n')
    out = doctor(vm, managed).stdout
    assert "git asks other credential helpers too (!%s git-credential; store)" % BOXEN in out
    # setup puts boxen's include last again
    vm.ok("setup")
    assert "ok    in %s, git asks only boxen" % vm.src in doctor(vm, managed).stdout
    assert not [f for f in os.listdir(vm.src) if f.startswith(".boxen-doctor")]
    # no ~/src at all: says so, and the rest of doctor still runs
    import shutil
    shutil.rmtree(vm.src)
    out = doctor(vm, managed).stdout
    assert "FAIL  can't check git's credential helpers: no %s (re-run install.sh)" % vm.src in out
    assert "\nProjects\n" in out and "rc=1" in out


def test_migrate_moved_but_not_started(v1):
    """A project whose build fails after the move is v2 now: the summary says
    to fix .boxen/ and `boxen up`, not to migrate again."""
    vm = v1
    vm.set_docker(fail=["build"])
    r = migrate(vm)
    assert r.returncode == 0
    assert "old: moved, but its build container didn't start (fix" in r.stderr
    assert "moved, but their build containers didn't start (fix .boxen/, then boxen up <name>): old" in r.stderr
    assert "not moved" not in r.stderr
    assert (vm.pdir("old") / "compose.yaml").read_text().startswith(FORMAT_MARK)
