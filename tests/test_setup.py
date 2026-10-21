"""`boxen setup`: the base image, Claude Code's settings (and the managed
settings with the sandbox), boxen.md, and git settings for ~/src. All in a
temp HOME."""
import json
import os
import stat
import subprocess

from conftest import BOXEN, REPO

BOXEN_MD = os.path.join(REPO, "templates", "boxen.md")


def settings(vm):
    return json.loads((vm.home / ".claude" / "settings.json").read_text())


def test_setup_fresh(vm):
    vm.ok("setup")
    calls = vm.docker_calls()
    assert ["compose", "version"] in calls
    assert ["build", "-t", "boxen-env:core", os.path.join(REPO, "image")] in calls
    assert ["tag", "boxen-env:core", "boxen-env:latest"] in calls
    # your settings get two defaults, nothing of the sandbox (that's managed)
    assert settings(vm) == {"cleanupPeriodDays": 365, "remoteControlAtStartup": True}
    c = vm.home / ".claude"
    assert (c / "CLAUDE.md").read_text() == "@boxen.md\n"
    assert (c / "boxen.md").read_text() == open(BOXEN_MD).read()
    assert (c / "skills" / "carry" / "SKILL.md").read_text() == open(os.path.join(REPO, "skills", "carry",
                                                                                  "SKILL.md")).read()
    # no systemd user session here: cron refreshes tokens
    cron = (vm.fake / "crontab").read_text()
    assert cron.count("# boxen-tokens") == 2 and "tokens --quiet" in cron
    # boxen setup again changes nothing
    before = (c / "settings.json").read_text()
    vm.ok("setup")
    assert (c / "settings.json").read_text() == before
    assert (vm.fake / "crontab").read_text().count("# boxen-tokens") == 2


def test_setup_refuses_a_daemon_that_isnt_rootless(vm):
    vm.set_docker(rootless=False)
    r = vm.boxen("setup")
    assert r.returncode == 1 and "isn't rootless" in r.stderr
    assert not (vm.home / ".claude").exists()


def test_settings_keeps_yours(vm):
    """Only cleanupPeriodDays and remoteControlAtStartup, and only when
    unset; everything else in your settings.json is left as it is."""
    c = vm.home / ".claude"
    c.mkdir()
    mine = {"model": "opus", "sandbox": {"enabled": False, "excludedCommands": ["make *"]},
            "permissions": {"allow": ["Bash(ls)"]}, "remoteControlAtStartup": False, "cleanupPeriodDays": 30}
    (c / "settings.json").write_text(json.dumps(mine))
    os.chmod(c / "settings.json", 0o640)
    vm.ok("setup")
    assert settings(vm) == mine                                          # nothing to add
    mine2 = {"model": "opus", "cleanupPeriodDays": 0, "permissions": {"deny": ["Read(~/.aws)"]}}
    (c / "settings.json").write_text(json.dumps(mine2))
    vm.ok("setup")
    assert settings(vm) == dict(mine2, remoteControlAtStartup=True)     # 0 is a value: kept
    assert stat.S_IMODE((c / "settings.json").stat().st_mode) == 0o640
    assert not [f for f in os.listdir(c) if f.startswith(".settings")]
    # CLAUDE_CONFIG_DIR is honored
    other = vm.tmp / "cc"
    vm.ok("setup", env={"CLAUDE_CONFIG_DIR": str(other)})
    assert json.loads((other / "settings.json").read_text())["cleanupPeriodDays"] == 365


def test_settings_refuses_non_object(vm):
    c = vm.home / ".claude"
    c.mkdir()
    for text in ("[1, 2]\n", "\"x\"\n", "{not json\n", "null\n"):
        (c / "settings.json").write_text(text)
        r = vm.ok("setup")
        assert "settings.json isn't a JSON object; leaving it alone" in r.stderr, text
        assert (c / "settings.json").read_text() == text
    # an empty file is a fresh start
    (c / "settings.json").write_text("")
    vm.ok("setup")
    assert settings(vm)["cleanupPeriodDays"] == 365


def test_managed_settings(vm):
    r = vm.ok("managed-settings")
    m = json.loads(r.stdout)
    uid = os.getuid()
    assert m == {
        "sandbox": {
            "enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
            "excludedCommands": ["boxen run *", "boxen env *"],
            "network": {"allowedDomains": ["github.com", "api.github.com", "*.githubusercontent.com"]},
            "filesystem": {"denyRead": [str(vm.run_dir), "/tmp/tmux-%d" % uid, "/var/run/docker.sock",
                                        "/run/docker.sock"]},
            "credentials": {
                "files": [{"path": "~/.ssh", "mode": "deny"}, {"path": "~/.config/gh", "mode": "deny"},
                          {"path": str(vm.bhome / "github"), "mode": "deny"}],
                "envVars": [{"name": n, "mode": "deny"} for n in
                            ("GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")]}},
        "permissions": {"allow": ["Bash(boxen run *)", "Bash(boxen env *)"],
                        "disableBypassPermissionsMode": "disable"}}
    # the runtime folder covers rootless Docker's socket
    assert (vm.run_dir / "docker.sock").parent == vm.run_dir
    # no XDG_RUNTIME_DIR: /run/user/<uid>
    env = dict(vm.env)
    del env["XDG_RUNTIME_DIR"]
    r = subprocess.run([BOXEN, "managed-settings"], env=env, capture_output=True, text=True, check=True)
    assert json.loads(r.stdout)["sandbox"]["filesystem"]["denyRead"][0] == "/run/user/%d" % uid
    # needs nothing else: no Docker, no project
    vm.set_docker(down=True)
    assert json.loads(vm.ok("managed-settings", cwd=vm.tmp).stdout) == m


def test_install_sh_installs_managed_settings():
    text = open(os.path.join(REPO, "install.sh")).read()
    assert '"$REPO_DIR/bin/boxen" managed-settings > "$tmp"' in text
    assert 'managed=/etc/claude-code/managed-settings.d/boxen.json' in text
    assert 'sudo install -D -m 0644 "$tmp" "$managed"' in text
    # after setup (which needs nothing of it), before the ui
    assert text.index('"$REPO_DIR/bin/boxen" setup') < text.index("managed-settings > ") < \
        text.index('"$REPO_DIR/ui/install-venv.sh"')
    assert 'MANAGED="/etc/claude-code/managed-settings.d/boxen.json"' in open(BOXEN).read()


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


def test_setup_with_systemd_user_services(vm):
    """With a systemd user session: the token timer and the serve service,
    and cron lines from an earlier boxen are dropped."""
    from conftest import write_exe
    write_exe(str(vm.bin / "systemctl"), '#!/bin/sh\necho "$*" >> "$FAKE_DIR/systemctl.log"\n')
    (vm.fake / "crontab").write_text("0 1 * * * mine\n*/30 * * * * old boxen tokens # boxen-tokens\n")
    vm.ok("setup")
    u = vm.home / ".config" / "systemd" / "user"
    svc = (u / "boxen-tokens.service").read_text()
    assert 'Environment="BOXEN_HOME=%s" "BOXEN_SRC=%s"' % (vm.bhome, vm.src) in svc
    assert "ExecStart=/bin/sh -c 'exec \"$0\" tokens --quiet >>\"$1\" 2>&1' \"%s\" \"%s\"" % (
        BOXEN, vm.bhome / "tokens.log") in svc
    assert "OnUnitActiveSec=30min" in (u / "boxen-tokens.timer").read_text()
    assert "ExecStart=/bin/bash -lc 'exec \"$0\" serve --restore' \"%s\"" % BOXEN in \
        (u / "boxen-serve.service").read_text()
    assert "After=" not in (u / "boxen-serve.service").read_text()
    log = (vm.fake / "systemctl.log").read_text().splitlines()
    assert "--user enable --now boxen-tokens.timer" in log and "--user enable boxen-serve.service" in log
    # deferred rebuilds: every 2 minutes, logged to rebuild.log
    assert "ExecStart=/bin/sh -c 'exec \"$0\" rebuild-pending >>\"$1\" 2>&1' \"%s\" \"%s\"" % (
        BOXEN, vm.bhome / "rebuild.log") in (u / "boxen-rebuild.service").read_text()
    assert "OnUnitActiveSec=2min" in (u / "boxen-rebuild.timer").read_text()
    assert "--user enable --now boxen-rebuild.timer" in log
    assert (vm.fake / "crontab").read_text() == "0 1 * * * mine\n"
    # unchanged units aren't rewritten
    m = (u / "boxen-tokens.service").stat().st_mtime_ns
    vm.ok("setup")
    assert (u / "boxen-tokens.service").stat().st_mtime_ns == m


def test_gitconfig_include_stays_last(vm):
    """Credential helpers added to ~/.gitconfig after boxen's include (yours,
    or gh auth setup-git's) would come after boxen's: setup moves the
    include back to the end, and in a project git asks only boxen."""
    vm.ok("setup")
    gcfg = vm.home / ".gitconfig"
    inc = "includeIf.gitdir:%s/.path" % vm.src
    with open(gcfg, "a") as f:
        f.write('[credential]\n\thelper = "!echo password=USERS; true"\n'
                '[credential "https://github.com"]\n\thelper =\n\thelper = "!echo password=GH; true"\n')

    def helpers(cwd):
        return subprocess.run(["git", "config", "--get-all", "credential.https://github.com.helper"], cwd=cwd,
                              env=vm.env, capture_output=True, text=True).stdout.splitlines()
    a = vm.src / "beta"
    subprocess.run(["git", "init", "-q", str(a)], check=True)
    assert helpers(a)[-1] == "!echo password=GH; true"                  # gh's wins: the problem
    r = vm.ok("setup")
    assert "repos under %s use boxen's git settings" % vm.src in r.stdout
    text = gcfg.read_text()
    assert text.rstrip().endswith("path = %s" % (vm.bhome / "gitconfig"))
    assert git_global(vm, "--get-all", inc) == str(vm.bhome / "gitconfig") + "\n"     # once
    assert helpers(a)[-2:] == ["", "!%s git-credential" % BOXEN]
    # beta has no token: boxen says quit, so git asks nobody else (not your login)
    fill = subprocess.run(["git", "credential", "fill"], cwd=a, env=dict(vm.env, GIT_TERMINAL_PROMPT="0"),
                          input="url=https://github.com/a/b\n\n", capture_output=True, text=True)
    assert fill.returncode != 0 and "USERS" not in fill.stdout and "GH" not in fill.stdout
    # last already (git reports it in lower case): left alone
    r = vm.ok("setup")
    assert "use boxen's git settings" not in r.stdout and gcfg.read_text() == text
    # outside ~/src your helpers are yours
    out = vm.tmp / "elsewhere"
    subprocess.run(["git", "init", "-q", str(out)], check=True)
    assert helpers(out)[-1] == "!echo password=GH; true"


def test_install_cron_defers_to_the_timer(vm):
    """With the boxen-tokens timer installed (from a systemd session), a run
    without systemd doesn't add cron lines."""
    t = vm.home / ".config" / "systemd" / "user"
    t.mkdir(parents=True)
    (t / "boxen-tokens.timer").write_text("[Timer]\n")
    r = vm.fn("install_cron && echo rc=0")
    assert r.stdout.endswith("rc=0\n") and not (vm.fake / "crontab").exists()
    (t / "boxen-tokens.timer").unlink()
    r = vm.fn("install_cron && echo rc=0")
    assert r.stdout.endswith("rc=0\n") and "# boxen-tokens" in (vm.fake / "crontab").read_text()


def apt_missing(vm, installed, *pkgs):
    """install.sh's apt_missing, with a fake dpkg that knows INSTALLED."""
    from conftest import write_exe
    text = open(os.path.join(REPO, "install.sh")).read()
    fn = text[text.index("apt_missing() {"):]
    fn = fn[:fn.index("\n}\n") + 3]
    d = vm.tmp / "dpkg-bin"
    d.mkdir(exist_ok=True)
    write_exe(str(d / "dpkg"), '#!/bin/sh\ncase " %s " in *" $2 "*) exit 0 ;; esac\nexit 1\n' % " ".join(installed))
    prog = "set -euo pipefail\n%s\nmapfile -t missing < <(apt_missing %s)\necho \"${#missing[@]}\"\n" \
           "apt_missing %s | od -c | head -3\n" % (fn, " ".join(pkgs), " ".join(pkgs))
    return subprocess.run(["bash", "-c", prog], env=dict(vm.env, PATH="%s:%s" % (d, vm.env["PATH"])),
                          capture_output=True, text=True)


def test_install_apt_missing(vm):
    r = apt_missing(vm, ["curl", "git"], "curl", "git")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "0\n0000000\n"           # nothing at all (not an empty line): no bytes
    r = apt_missing(vm, ["curl"], "curl", "git", "jq")
    lines = r.stdout.splitlines()
    assert lines[0] == "2" and "g   i   t  \\n   j   q  \\n" in r.stdout
