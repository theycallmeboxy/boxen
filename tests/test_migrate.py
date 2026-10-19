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


def migrate(vm, *args):
    script = 'source "$BOXEN_REPO/lib/migrate.sh"\nV1_SOCK=%s\nmigrate_main %s\n' % (
        vm.tmp / "system-docker.sock", " ".join(args))
    return vm.fn(script)


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
    assert ["-H", "unix://%s" % (vm.tmp / "system-docker.sock"), "stop", "boxen-old"] in [c["argv"] for c in log]
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
    assert "old: %s doesn't exist; skipping" % (v1.src / "old") in r.stderr
    assert "these need a look: old" in r.stderr
    assert (v1.pdir("old") / "compose.yaml").read_text().startswith("name: boxen-old")     # untouched


def test_migrate_needs_the_system_daemon(vm):
    r = vm.fn('source "$BOXEN_REPO/lib/migrate.sh"\nV1_SOCK=%s\nmigrate_main\n' % (vm.tmp / "nosock"))
    assert r.returncode == 1 and "can't reach the system Docker daemon, where v1 lives" in r.stderr
    r = vm.boxen("migrate", "now")
    assert r.returncode == 1 and "usage: boxen migrate [--cleanup]" in r.stderr
    r = vm.boxen("migrate", "--cleanup")
    assert r.returncode == 1 and "these v1 projects haven't moved yet: old" in r.stderr


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
