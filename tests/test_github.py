"""GitHub: repo lists (`boxen repos`), tokens per owner (`boxen tokens`),
seeding from origin and from `new`'s clone URL, discovery (boxen-gitscan),
with a fake GitHub API (curl)."""
import json
import os
import subprocess
import time

import pytest

from conftest import REPO, py_exe, FAKE_CURL

INSTALLS = [{"login": "TheyCallMeBoxy", "id": 11, "repos": ["alpha", "beta", "gamma"]},
            {"login": "myorg", "id": 22, "repos": "all"}]


@pytest.fixture
def gh(vm):
    """vm with a configured GitHub App (a real key, so make_jwt works) and
    a fake GitHub API."""
    gdir = vm.bhome / "github"
    gdir.mkdir(parents=True)
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                    "-out", str(gdir / "app.pem")], check=True, capture_output=True)
    (gdir / "app.conf").write_text("CLIENT_ID=Iv1.test\nAPP_SLUG=boxen-test\n")
    py_exe(vm.bin / "curl", FAKE_CURL)
    (vm.fake / "github.json").write_text(json.dumps({"installations": INSTALLS}))
    vm.tok = vm.pdir("alpha") / "token"
    return vm


def api_calls(vm):
    p = vm.fake / "curl.jsonl"
    out = [json.loads(line) for line in open(p)] if p.exists() else []
    p.unlink(missing_ok=True)
    return [(c["method"], c["url"].replace("https://api.github.com", ""), c["body"]) for c in out]


def listed(vm, name="alpha"):
    text = (vm.pdir(name) / "repos").read_text()
    return [x for x in text.splitlines() if x and not x.startswith("#")]


def state(vm, name="alpha"):
    return json.loads((vm.pdir(name) / "token" / "state.json").read_text())


def tokens(vm, name="alpha"):
    d = vm.pdir(name) / "token"
    return {p.name: p.read_text().strip() for p in sorted(d.iterdir()) if p.name.startswith("gh-token")}


def git(*args):
    subprocess.run(["git"] + list(args), check=True, capture_output=True)


def ls_project(vm, name="alpha"):
    doc = json.loads(vm.ok("ls", "--json").stdout)
    return [p for p in doc["projects"] if p["name"] == name][0]


def test_jwt(gh):
    r = gh.fn("make_jwt")
    assert r.returncode == 0, r.stderr
    h, p, s = r.stdout.split(".")
    pad = lambda x: x + "=" * (-len(x) % 4)  # noqa: E731
    import base64
    assert json.loads(base64.urlsafe_b64decode(pad(h))) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(base64.urlsafe_b64decode(pad(p)))
    assert claims["iss"] == "Iv1.test" and claims["exp"] - claims["iat"] == 600
    assert abs(claims["iat"] + 60 - time.time()) < 30


def test_seed_lazily_from_origin_then_origin_is_ignored(gh):
    r = gh.ok("tokens", "alpha")
    assert listed(gh) == ["theycallmeboxy/alpha"]
    assert "theycallmeboxy/alpha" in r.stdout and "token valid until 2026-10-19T12:00:00Z" in r.stdout
    assert tokens(gh) == {"gh-token": "tok-theycallmeboxy-alpha", "gh-token.theycallmeboxy": "tok-theycallmeboxy-alpha"}
    assert state(gh) == {"theycallmeboxy/alpha": "ok"}
    for f in gh.tok.iterdir():
        assert f.stat().st_mode & 0o777 == 0o600, f
    assert gh.tok.stat().st_mode & 0o777 == 0o700
    # The project's agent points origin at another repo the app could reach:
    # its token doesn't follow.
    git("-C", str(gh.src / "alpha"), "remote", "set-url", "origin", "https://github.com/myorg/secret.git")
    api_calls(gh)
    gh.ok("tokens", "alpha")
    assert listed(gh) == ["theycallmeboxy/alpha"]
    assert not any("secret" in json.dumps(c) for c in api_calls(gh))
    assert "gh-token.myorg" not in tokens(gh)
    p = ls_project(gh)
    assert p["repos"] == [{"repo": "theycallmeboxy/alpha", "state": "ok", "origin": False}]
    assert p["github"] == "token ok"
    r = gh.ok("repos", "alpha")
    assert "origin myorg/secret isn't listed" in r.stdout
    assert "Found in the project folder but not listed:\n    myorg/secret\n" in r.stdout


def test_tokens_for_every_project(gh):
    """Every v2 project, and the v1 ones until they're migrated (their
    containers read the same token files), but not v1's supervisor."""
    subprocess.run(["git", "-C", str(gh.src / "old"), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(gh.src / "old"), "remote", "add", "origin",
                    "https://github.com/myorg/old.git"], check=True)
    sup = gh.pdir("supervisor")
    sup.mkdir()
    (sup / "compose.yaml").write_text("name: boxen-supervisor\n")
    r = gh.ok("tokens")
    assert listed(gh) == ["theycallmeboxy/alpha"]
    assert listed(gh, "beta") == []                     # no origin: an empty list
    assert listed(gh, "old") == ["myorg/old"]
    assert tokens(gh, "old") == {"gh-token": "tok-myorg-old", "gh-token.myorg": "tok-myorg-old"}
    assert not (sup / "repos").exists() and not (sup / "token").exists()
    assert "old" in r.stdout and "supervisor" not in r.stdout
    # the timer's run covers them too
    (gh.pdir("old") / "token" / "gh-token").unlink()
    gh.ok("tokens", "--quiet")
    assert (gh.pdir("old") / "token" / "gh-token").exists()
    assert "beta" in r.stdout and "no GitHub repos listed" in r.stdout
    # a list that exists is never reseeded
    (gh.pdir("alpha") / "repos").write_text("myorg/x\n")
    gh.ok("tokens")
    assert listed(gh) == ["myorg/x"]
    # quiet (the timer): nothing on stdout when all is well
    r = gh.ok("tokens", "--quiet")
    assert r.stdout == "" and r.stderr == ""
    r = gh.boxen("tokens", "old")
    assert r.returncode == 1 and "boxen v1 project" in r.stderr


def test_tokens_without_app(vm):
    r = vm.ok("tokens")
    assert "GitHub isn't set up yet" in r.stderr
    r = vm.ok("tokens", "--quiet")
    assert r.stderr == ""


def test_new_from_clone_url_mints_and_clones_with_the_token(gh):
    r = gh.ok("new", "gamma", "https://github.com/TheyCallMeBoxy/gamma.git")
    assert listed(gh, "gamma") == ["TheyCallMeBoxy/gamma"]
    assert tokens(gh, "gamma")["gh-token.theycallmeboxy"] == "tok-theycallmeboxy-gamma"
    (clone,) = gh.git_log()
    # the fake git asked the credential helper on the clone's command line
    assert clone["cred"] == "username=x-access-token\npassword=tok-theycallmeboxy-gamma\n"
    assert "cloning https://github.com/TheyCallMeBoxy/gamma.git" in r.stdout
    # someone else's repo: listed (not-installed), no token, the clone still runs
    r = gh.ok("new", "delta", "https://github.com/upstream/delta")
    assert listed(gh, "delta") == ["upstream/delta"]
    assert "isn't yours" in r.stdout and "boxen repos delta add" in r.stdout
    assert state(gh, "delta") == {"upstream/delta": "not-installed"}
    assert gh.git_log()[-1]["cred"] == "quit=1\n"
    # yours but the app can't reach it: refused, cleaned up
    r = gh.boxen("new", "eps", "https://github.com/theycallmeboxy/private")
    assert r.returncode == 1 and "no GitHub token for theycallmeboxy/private, so it can't be cloned" in r.stderr
    assert not gh.pdir("eps").exists() and not (gh.src / "eps").exists()


def test_repos_add_rm_and_validation(gh):
    gh.ok("tokens", "alpha")
    for bad in ("a", "a/b/c", "-x/y", "a/../b", "https://gitlab.com/a/b", "a/b;rm", "a/..", "a /b"):
        r = gh.boxen("repos", "alpha", "add", bad)
        assert r.returncode == 1 and ("isn't a GitHub repo" in r.stderr or "unknown option" in r.stderr), bad
    assert gh.boxen("repos", "alpha", "add").returncode == 1
    assert gh.boxen("repos", "alpha", "frob", "a/b").returncode == 1
    assert gh.boxen("repos", "old").returncode == 1
    assert listed(gh) == ["theycallmeboxy/alpha"]
    r = gh.ok("repos", "alpha", "add", "https://github.com/myorg/x.git", "myorg/Y", "MyOrg/x")
    assert listed(gh) == ["theycallmeboxy/alpha", "myorg/x", "myorg/Y"]
    assert "myorg/x                                  ok" in r.stdout
    assert tokens(gh)["gh-token.myorg"] == "tok-myorg-x,Y"
    # already listed (any case): nothing added, but GitHub is asked again at
    # once (after a fix there, the state shouldn't wait for the timer)
    (gh.pdir("alpha") / "token" / "state.json").write_text('{"myorg/x": "no-access"}')
    r = gh.ok("repos", "alpha", "add", "MYORG/X")
    assert "already listed: MYORG/X; checking GitHub again" in r.stdout
    assert state(gh)["myorg/x"] == "ok"
    assert "myorg/x                                  ok" in r.stdout
    # rm keeps other lines (comments too) as they were
    text = (gh.pdir("alpha") / "repos").read_text()
    (gh.pdir("alpha") / "repos").write_text(text + "# my note\n")
    gh.ok("repos", "alpha", "rm", "myorg/x", "MyOrg/y")
    assert listed(gh) == ["theycallmeboxy/alpha"]
    assert (gh.pdir("alpha") / "repos").read_text() == text.replace("myorg/x\nmyorg/Y\n", "") + "# my note\n"
    assert "gh-token.myorg" not in tokens(gh)        # that owner's token is gone
    assert state(gh) == {"theycallmeboxy/alpha": "ok"}
    r = gh.boxen("repos", "alpha", "rm", "nobody/here")
    assert "nobody/here isn't listed" in r.stderr
    # --suggest
    assert gh.ok("repos", "alpha", "--suggest").stdout == ""
    assert gh.boxen("repos", "alpha", "--suggest", "x").returncode == 1


def test_repos_without_app(vm):
    r = vm.ok("repos", "alpha", "add", "myorg/x")
    assert "GitHub isn't set up yet ('boxen github'): no tokens until it is" in r.stderr
    assert listed(vm) == ["theycallmeboxy/alpha", "myorg/x"]        # seeded from origin first
    assert "myorg/x                                  run boxen github" in r.stdout
    assert not (vm.pdir("alpha") / "token" / "state.json").exists()


def test_repos_ignores_bad_lines_and_links(gh):
    (gh.pdir("alpha") / "repos").write_text("theycallmeboxy/alpha\nnot a repo\n  myorg/x.git  # mine\nMYORG/X\n")
    r = gh.ok("repos", "alpha")
    assert "    ignored (not owner/repo): not a repo" in r.stdout
    r = gh.fn("listed_repos alpha")
    assert r.stdout == "theycallmeboxy/alpha\nmyorg/x\n"
    os.unlink(gh.pdir("alpha") / "repos")
    os.symlink("/etc/passwd", gh.pdir("alpha") / "repos")
    assert gh.fn("listed_repos alpha").stdout == ""


def test_minting_one_request_per_owner(gh):
    (gh.pdir("alpha") / "repos").write_text("TheyCallMeBoxy/alpha\nmyorg/one\ntheycallmeboxy/beta\nmyorg/two\n")
    gh.tok.mkdir()
    (gh.tok / "gh-token.gone").write_text("old\n")          # an owner no longer listed
    gh.ok("tokens", "alpha")
    calls = api_calls(gh)
    posts = [c for c in calls if c[0] == "POST"]
    assert posts == [("POST", "/app/installations/11/access_tokens", {"repositories": ["alpha", "beta"]}),
                     ("POST", "/app/installations/22/access_tokens", {"repositories": ["one", "two"]})]
    assert [c[1] for c in calls if c[0] == "GET"] == ["/app/installations?per_page=100"]    # asked once
    assert tokens(gh) == {"gh-token": "tok-theycallmeboxy-alpha,beta",
                          "gh-token.myorg": "tok-myorg-one,two",
                          "gh-token.theycallmeboxy": "tok-theycallmeboxy-alpha,beta"}
    assert state(gh) == {"theycallmeboxy/alpha": "ok", "myorg/one": "ok", "theycallmeboxy/beta": "ok",
                         "myorg/two": "ok"}


def test_partial_failure_retries_per_repo(gh):
    (gh.pdir("alpha") / "repos").write_text("upstream/thing\ntheycallmeboxy/alpha\ntheycallmeboxy/private\n")
    r = gh.ok("tokens", "alpha")
    assert "theycallmeboxy/private: the boxen GitHub App can't access it" in r.stderr
    calls = api_calls(gh)
    assert [c for c in calls if c[0] == "POST"] == [
        ("POST", "/app/installations/11/access_tokens", {"repositories": ["alpha", "private"]}),
        ("POST", "/app/installations/11/access_tokens", {"repositories": ["alpha"]})]
    assert ("GET", "/repos/theycallmeboxy/private/installation", None) in calls
    assert not any("upstream" in c[1] for c in calls)               # not installed there: nothing asked
    assert state(gh) == {"upstream/thing": "not-installed", "theycallmeboxy/alpha": "ok",
                         "theycallmeboxy/private": "no-access"}
    # the default is the first listed repo's owner that has a token
    assert tokens(gh) == {"gh-token": "tok-theycallmeboxy-alpha", "gh-token.theycallmeboxy": "tok-theycallmeboxy-alpha"}
    assert "not your repo" in r.stdout and "no access" in r.stdout
    p = ls_project(gh)
    assert p["github"] == "token partial" and p["repo"] == "upstream/thing"
    assert [(x["repo"], x["state"], x["origin"]) for x in p["repos"]] == [
        ("upstream/thing", "not-installed", False), ("theycallmeboxy/alpha", "ok", True),
        ("theycallmeboxy/private", "no-access", False)]
    assert "token partial" in gh.ok("ls").stdout
    # GitHub unreachable: errors, and the token there is kept
    (gh.fake / "github.json").write_text(json.dumps({"installations": INSTALLS, "down": True}))
    r = gh.boxen("tokens", "--quiet", "alpha")
    assert r.returncode == 1
    assert tokens(gh)["gh-token.theycallmeboxy"] == "tok-theycallmeboxy-alpha"
    assert state(gh)["theycallmeboxy/alpha"] == "error"


def test_github_column(gh):
    assert ls_project(gh)["github"] == "-"                      # nothing listed yet
    (gh.pdir("alpha") / "repos").write_text("upstream/x\n")
    gh.ok("tokens", "alpha")
    assert ls_project(gh)["github"] == "no token"
    (gh.pdir("alpha") / "repos").write_text("theycallmeboxy/alpha\n")
    gh.ok("tokens", "alpha")
    assert ls_project(gh)["github"] == "token ok"
    old = time.time() - 3600
    for f in gh.tok.iterdir():
        os.utime(f, (old, old))
    assert ls_project(gh)["github"] == "token stale"
    # no app: says so
    os.unlink(gh.bhome / "github" / "app.pem")
    assert ls_project(gh)["github"] == "run boxen github"


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_discovery_reads_configs_as_text(gh):
    a = gh.src / "alpha"
    pwned = gh.tmp / "pwned"
    secret = gh.tmp / "secret.cfg"
    secret.write_text('[remote "x"]\n\turl = https://github.com/leak/fromvm\n')
    with open(a / ".git" / "config", "a") as f:
        f.write('[core]\n\tfsmonitor = touch %s\n[include]\n\tpath = %s\n[includeIf "gitdir:/"]\n\tpath = %s\n'
                '[remote "fork"]\n\turl = git@github.com:fork/alpha.git\n\tpushurl = "https://github.com/fork/push" ; c\n'
                % (pwned, secret, secret))
    git("init", "-q", str(a / "libs" / "nested"))
    git("-C", str(a / "libs" / "nested"), "remote", "add", "up", "https://github.com/nested/lib")
    write(a / ".gitmodules", '[submodule "sub"]\n\tpath = sub\n\turl = ../sibling.git\n')
    write(a / ".git" / "modules" / "sub" / "config", '[remote "origin"]\n\turl = https://github.com/mod/sub\n')
    write(a / "sub" / ".git", "gitdir: ../.git/modules/sub\n")
    git("init", "-q", str(a / "node_modules" / "dep"))
    git("-C", str(a / "node_modules" / "dep"), "remote", "add", "origin", "https://github.com/heavy/dep")
    git("init", "-q", str(a / "a" / "b" / "c" / "d"))         # deeper than 3
    git("-C", str(a / "a" / "b" / "c" / "d"), "remote", "add", "origin", "https://github.com/deep/repo")
    os.symlink(gh.tmp, a / "link")                             # never followed
    write(a / "evil" / ".git", "gitdir: %s\n" % gh.tmp)        # pointing outside: never read
    write(gh.tmp / "config", '[remote "o"]\n\turl = https://github.com/leak/outside\n')
    write(a / "junk" / ".git" / "config", '[remote "o"]\n\turl = https://github.com/[bold]x/y\n'
                                          '[remote "p"]\n\turl = https://evil.com/x@github.com/a/b\n')
    gh.ok("tokens", "alpha")                                   # seeds the list from origin
    r = gh.ok("repos", "alpha", "--suggest")
    assert r.stdout.split() == ["fork/alpha", "fork/push", "theycallmeboxy/sibling", "nested/lib", "mod/sub"]
    assert not pwned.exists()
    full = gh.ok("repos", "alpha").stdout
    assert "Found in the project folder but not listed:" in full and "leak" not in full
    (gh.pdir("alpha") / "repos").write_text("theycallmeboxy/alpha\nfork/alpha\n")
    assert gh.ok("repos", "alpha", "--suggest").stdout.split() == ["fork/push", "theycallmeboxy/sibling",
                                                                    "nested/lib", "mod/sub"]
    assert not pwned.exists()


def test_discovery_time_box(gh):
    a = gh.src / "alpha"
    for i in range(300):
        (a / ("d%d" % i) / "x" / "y").mkdir(parents=True)
    t = time.monotonic()
    r = subprocess.run([os.path.join(REPO, "bin", "boxen-gitscan"), "scan", str(a)], capture_output=True,
                       text=True, env=dict(gh.env, BOXEN_SCAN_SECONDS="0.01"))
    assert r.returncode == 0 and time.monotonic() - t < 2
    assert r.stdout.split()[:1] == ["theycallmeboxy/alpha"]


def test_gitscan_origin(gh):
    g = os.path.join(REPO, "bin", "boxen-gitscan")
    r = subprocess.run([g, "origin", str(gh.src / "alpha"), str(gh.src / "beta"), str(gh.tmp / "nosuch")],
                       capture_output=True, text=True)
    assert r.stdout == "theycallmeboxy/alpha\n\n\n"
    r = subprocess.run([g], capture_output=True, text=True)
    assert r.returncode == 2 and "usage" in r.stderr


def test_github_repo_parsing(gh):
    cases = {"https://github.com/a/b.git": "a/b", "https://github.com/a/b/": "a/b", "git@github.com:A/b-c": "A/b-c",
             "ssh://git@github.com/a/b.git": "a/b", "https://user@github.com/a/b": "a/b",
             "https://github.com/a/b/c": None, "https://github.com.evil/a/b": None, "http://github.com/a/b": None,
             "https://u/x@github.com/a/b": None, "https://github.com/a/..": None, "https://github.com/-a/b": None}
    for url, want in cases.items():
        r = gh.fn("github_repo '%s' || echo NONE" % url)
        assert r.stdout == (want or "NONE") + "\n", url


def test_app_manifest_permissions():
    """The GitHub App's pre-filled permissions (cmd_github's URL)."""
    text = open(os.path.join(REPO, "lib", "github.sh")).read()
    for perm in ("contents=write", "pull_requests=write", "issues=write", "workflows=write", "actions=read",
                 "checks=read", "statuses=read", "metadata=read"):
        assert "&" + perm in text, perm
