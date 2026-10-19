"""lib/migrate.sh: moving a boxen v1 install (agents in containers on the
system Docker daemon) to v2, with a fake v1 daemon behind `docker -H`."""
import json
import os
import subprocess

import pytest

from conftest import FORMAT_MARK, REPO, unix_socket

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
                "settings.json": json.dumps({"model": "opus", "permissions": {"allow": ["Bash(npm *)"]},
                                             "theme": "v1"}),
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
    assert s["model"] == "opus" and s["theme"] == "vm"                  # v1's keys, the VM's values win
    assert s["permissions"]["allow"][:2] == ["Bash(npm *)", "Bash(boxen run *)"]
    assert s["sandbox"]["enabled"] is True                              # and boxen's settings on top
    assert json.loads((c / "settings.json.v1").read_text())["theme"] == "v1"
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


def test_doctor_runs(vm):
    """doctor runs every check with fakes and reports (some fail here: the
    fake Docker writes no files, there's no Claude Code login)."""
    vm.ok("setup")
    r = vm.boxen("doctor")
    assert r.returncode in (0, 1), r.stderr
    for head in ("Rootless Docker (build containers)", "Claude Code and its sandbox (agents)", "GitHub", "Projects"):
        assert head in r.stdout
    assert "the daemon at %s is rootless" % vm.env["BOXEN_DOCKER_HOST"] in r.stdout
    assert "settings: sandbox on and required" in r.stdout
    assert "settings: boxen run and boxen env are the sandbox's only exceptions" in r.stdout
    assert "settings: sandboxed commands can't open the Docker socket" in r.stdout
    assert "agents get boxen's instructions" in r.stdout
    assert "git in %s uses boxen's tokens and identity" % vm.src in r.stdout
    assert "boxen v1 projects to move: old" in r.stdout
    assert "2 v2 project(s)" in r.stdout
    r = vm.boxen("doctor", "--frob")
    assert r.returncode == 1 and "usage: boxen doctor" in r.stderr
