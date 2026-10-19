"""`boxen setup`: the base image, Claude Code's settings (the sandbox),
boxen.md, and git settings for ~/src. All in a temp HOME."""
import json
import os
import stat
import subprocess

from conftest import BOXEN, REPO

BOXEN_MD = os.path.join(REPO, "templates", "boxen.md")


def settings(vm):
    return json.loads((vm.home / ".claude" / "settings.json").read_text())


def test_setup_fresh(vm):
    r = vm.ok("setup")
    calls = vm.docker_calls()
    assert ["compose", "version"] in calls
    assert ["build", "-t", "boxen-env:core", os.path.join(REPO, "image")] in calls
    assert ["tag", "boxen-env:core", "boxen-env:latest"] in calls
    s = settings(vm)
    assert s["sandbox"]["enabled"] is True
    assert s["sandbox"]["failIfUnavailable"] is True
    assert s["sandbox"]["allowUnsandboxedCommands"] is False
    assert s["sandbox"]["excludedCommands"] == ["boxen run *", "boxen env *"]
    assert s["sandbox"]["network"]["allowedDomains"] == ["github.com", "api.github.com", "*.githubusercontent.com"]
    assert s["sandbox"]["filesystem"]["denyRead"] == [str(vm.run_dir / "docker.sock")]
    assert s["sandbox"]["credentials"]["files"] == [
        {"path": "~/.ssh", "mode": "deny"}, {"path": "~/.config/gh", "mode": "deny"},
        {"path": str(vm.bhome / "github"), "mode": "deny"}]
    assert s["permissions"]["allow"] == ["Bash(boxen run *)", "Bash(boxen env *)"]
    assert s["remoteControlAtStartup"] is True and s["cleanupPeriodDays"] == 365
    c = vm.home / ".claude"
    assert (c / "CLAUDE.md").read_text() == "@boxen.md\n"
    assert (c / "boxen.md").read_text() == open(BOXEN_MD).read()
    assert (c / "skills" / "carry" / "SKILL.md").read_text() == open(os.path.join(REPO, "skills", "carry",
                                                                                  "SKILL.md")).read()
    assert "Claude settings: sandbox on" in r.stdout
    # no systemd user session here: cron refreshes tokens
    cron = (vm.fake / "crontab").read_text()
    assert cron.count("# boxen-tokens") == 2 and "tokens --quiet" in cron
    # boxen setup again changes nothing
    before = (c / "settings.json").read_text()
    r = vm.ok("setup")
    assert (c / "settings.json").read_text() == before
    assert "Claude settings" not in r.stdout
    assert (vm.fake / "crontab").read_text().count("# boxen-tokens") == 2


def test_setup_refuses_a_daemon_that_isnt_rootless(vm):
    vm.set_docker(rootless=False)
    r = vm.boxen("setup")
    assert r.returncode == 1 and "isn't rootless" in r.stderr
    assert not (vm.home / ".claude").exists()


def test_settings_merge_keeps_yours(vm):
    c = vm.home / ".claude"
    c.mkdir()
    mine = {
        "model": "opus", "env": {"FOO": "1"},
        "sandbox": {"enabled": False, "allowUnsandboxedCommands": True, "excludedCommands": ["make *", "boxen run *"],
                    "network": {"allowedDomains": ["pypi.org", "github.com"], "allowUnixSockets": ["/x"]},
                    "filesystem": {"denyRead": ["~/secret"]}, "autoAllowBashIfSandboxed": True},
        "permissions": {"allow": ["Bash(ls)"], "deny": ["Read(~/.aws)"]},
        "remoteControlAtStartup": False, "cleanupPeriodDays": 30,
    }
    (c / "settings.json").write_text(json.dumps(mine))
    os.chmod(c / "settings.json", 0o640)
    vm.ok("setup")
    s = settings(vm)
    assert s["model"] == "opus" and s["env"] == {"FOO": "1"}
    assert s["remoteControlAtStartup"] is False and s["cleanupPeriodDays"] == 30
    sb = s["sandbox"]
    assert (sb["enabled"], sb["failIfUnavailable"], sb["allowUnsandboxedCommands"]) == (True, True, False)
    assert sb["autoAllowBashIfSandboxed"] is True
    assert sb["excludedCommands"] == ["make *", "boxen run *", "boxen env *"]
    assert sb["network"] == {"allowedDomains": ["pypi.org", "github.com", "api.github.com",
                                                "*.githubusercontent.com"], "allowUnixSockets": ["/x"]}
    assert sb["filesystem"]["denyRead"] == ["~/secret", str(vm.run_dir / "docker.sock")]
    assert s["permissions"] == {"allow": ["Bash(ls)", "Bash(boxen run *)", "Bash(boxen env *)"],
                                "deny": ["Read(~/.aws)"]}
    assert stat.S_IMODE((c / "settings.json").stat().st_mode) == 0o640
    assert not [f for f in os.listdir(c) if f.startswith(".settings")]
    # again: no duplicates, nothing changed
    before = (c / "settings.json").read_text()
    vm.ok("setup")
    assert (c / "settings.json").read_text() == before


def test_settings_refuses_non_object(vm):
    c = vm.home / ".claude"
    c.mkdir()
    for text in ("[1, 2]\n", "\"x\"\n", "{not json\n", "null\n"):
        (c / "settings.json").write_text(text)
        r = vm.ok("setup")
        assert "isn't a JSON object, so boxen's sandbox settings are NOT applied" in r.stderr, text
        assert (c / "settings.json").read_text() == text
    # an empty file is a fresh start
    (c / "settings.json").write_text("")
    vm.ok("setup")
    assert settings(vm)["sandbox"]["enabled"] is True


def test_settings_include_system_docker_sockets(vm):
    """Every Docker socket on the VM is denied: the rootless one, and the
    system one where it exists (checked with the function: this VM has none)."""
    r = vm.fn("claude_settings && cat \"$CLAUDE_DIR/settings.json\"")
    assert r.returncode == 0, r.stderr
    deny = json.loads(r.stdout[r.stdout.index("{"):])["sandbox"]["filesystem"]["denyRead"]
    want = [str(vm.run_dir / "docker.sock")] + [p for p in ("/var/run/docker.sock", "/run/docker.sock")
                                                if os.path.exists(p)]
    assert deny == want
    # CLAUDE_CONFIG_DIR is honored
    other = vm.tmp / "cc"
    vm.ok("setup", env={"CLAUDE_CONFIG_DIR": str(other)})
    assert json.loads((other / "settings.json").read_text())["sandbox"]["enabled"] is True


def test_claude_config(vm):
    c = vm.home / ".claude"
    c.mkdir()
    (c / "CLAUDE.md").write_text("# Mine\nbe nice")                   # no newline at the end
    vm.ok("setup")
    assert (c / "CLAUDE.md").read_text() == "# Mine\nbe nice\n\n@boxen.md\n"
    vm.ok("setup")
    assert (c / "CLAUDE.md").read_text().count("@boxen.md") == 1
    # an empty one
    (c / "CLAUDE.md").write_text("")
    vm.ok("setup")
    assert (c / "CLAUDE.md").read_text() == "\n@boxen.md\n"
    # boxen.md of your own is left alone
    (c / "boxen.md").write_text("my notes\n")
    r = vm.ok("setup")
    assert (c / "boxen.md").read_text() == "my notes\n"
    assert "boxen.md isn't boxen's (no boxen-managed line); leaving it alone" in r.stderr
    # boxen's own (an older one) is replaced
    (c / "boxen.md").write_text("<!-- boxen-managed: old -->\nold text\n")
    vm.ok("setup")
    assert (c / "boxen.md").read_text() == open(BOXEN_MD).read()
    # the same for the /carry skill
    sk = c / "skills" / "carry" / "SKILL.md"
    sk.write_text("---\nname: carry\n---\nmine\n")
    vm.ok("setup")
    assert sk.read_text().endswith("mine\n")


def git_global(vm, *args):
    return subprocess.run(["git", "config", "--global"] + list(args), env=vm.env, capture_output=True,
                          text=True).stdout


def test_gitconfig(vm):
    (vm.home / ".gitconfig").write_text("[user]\n\tname = Me\n\temail = me@example.com\n")
    r = vm.ok("setup")
    inc = "includeIf.gitdir:%s/.path" % vm.src
    gc = vm.bhome / "gitconfig"
    assert git_global(vm, "--get-all", inc) == str(gc) + "\n"
    assert "repos under %s use boxen's git settings" % vm.src in r.stdout
    assert git_global(vm, "user.name") == "Me\n"                 # yours stays
    b = lambda *a: subprocess.run(["git", "config", "-f", str(gc)] + list(a), capture_output=True,  # noqa: E731
                                  text=True).stdout
    assert b("--get-all", "credential.https://github.com.helper") == "\n!%s git-credential\n" % BOXEN
    assert b("credential.https://github.com.useHttpPath") == "true\n"
    assert b("--get-all", "url.https://github.com/.insteadOf") == "git@github.com:\nssh://git@github.com/\n"
    assert b("user.name") == ""
    assert stat.S_IMODE(gc.stat().st_mode) == 0o644
    # an identity set there (boxen github does) is kept by setup
    subprocess.run(["git", "config", "-f", str(gc), "user.name", "Boxy"], check=True)
    subprocess.run(["git", "config", "-f", str(gc), "user.email", "1+boxy@users.noreply.github.com"], check=True)
    r = vm.ok("setup")
    assert "use boxen's git settings" not in r.stdout
    assert git_global(vm, "--get-all", inc) == str(gc) + "\n"        # exactly once
    assert b("user.name") == "Boxy\n" and b("user.email") == "1+boxy@users.noreply.github.com\n"
    assert not [f for f in os.listdir(vm.bhome) if f.startswith(".gitconfig")]


def test_gitconfig_applies_in_projects(vm):
    """Real git: in a repo under ~/src the boxen identity and credential
    helper apply (the project's token, by the repo's owner); outside, not."""
    (vm.home / ".gitconfig").write_text("[user]\n\temail = me@example.com\n")
    vm.ok("setup")
    gc = vm.bhome / "gitconfig"
    subprocess.run(["git", "config", "-f", str(gc), "user.email", "boxen@example.com"], check=True)
    tok = vm.pdir("alpha") / "token"
    tok.mkdir()
    (tok / "gh-token").write_text("DEFAULT\n")
    (tok / "gh-token.myorg").write_text("ORG\n")
    a = vm.src / "alpha"

    def git(cwd, *args, input=None):
        return subprocess.run(["git"] + list(args), cwd=cwd, env=vm.env, capture_output=True, text=True,
                              input=input)
    assert git(a, "config", "user.email").stdout == "boxen@example.com\n"
    fill = git(a, "credential", "fill", input="url=https://github.com/MyOrg/x.git\n\n")
    assert "password=ORG\n" in fill.stdout, fill.stderr
    fill = git(a, "credential", "fill", input="url=https://github.com/theycallmeboxy/alpha\n\n")
    assert "password=DEFAULT\n" in fill.stdout
    # ssh URLs are fetched over https
    assert git(a, "ls-remote", "--get-url", "git@github.com:a/b.git").stdout == "https://github.com/a/b.git\n"
    # outside ~/src: your identity, no boxen helper
    outside = vm.tmp / "elsewhere"
    subprocess.run(["git", "init", "-q", str(outside)], check=True)
    assert git(outside, "config", "user.email").stdout == "me@example.com\n"
    assert git(outside, "config", "--get-all", "credential.https://github.com.helper").stdout == ""
