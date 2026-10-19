"""Where things are: the same rules as bin/boxen, read from the environment
when the app starts (tests point them at fakes)."""
import os
import re
from dataclasses import dataclass

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(os.path.dirname(PKG_DIR))

# Project names and GitHub URLs: the same rules as bin/boxen (valid_name,
# github_repo, repo_arg).
NAME_RE = re.compile(r"^[a-z0-9]+([_-][a-z0-9]+)*$")
_SEG = r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?"
GITHUB_URL_RE = re.compile(r"^https://github\.com/%s/%s(\.git)?$" % (_SEG, _SEG))
REPO_RE = re.compile(r"^%s/%s$" % (_SEG, _SEG))
RESERVED = ("all", "global", "dashboard", "ui")


def valid_name(name):
    return bool(NAME_RE.match(name or "")) and len(name) <= 40


def name_problem(name):
    """Why name can't be a new project, or None."""
    if not name:
        return "a name is required"
    if not valid_name(name):
        return "lowercase letters and digits, single '-' or '_' between them, max 40 chars"
    if name in RESERVED:
        return "'%s' is reserved" % name
    return None


def url_problem(url):
    """Why url can't be cloned by `boxen new`, or None (empty is fine)."""
    if not url:
        return None
    if not GITHUB_URL_RE.match(url) or ".." in url:
        return "only https://github.com/owner/repo URLs (nobody here has SSH keys)"
    return None


def repo_arg(s):
    """owner/repo from owner/repo or https://github.com/owner/repo[.git], as
    bin/boxen's repo_arg normalizes it, else None."""
    if not isinstance(s, str) or not s or any(c.isspace() for c in s):
        return None
    u = s if "://" in s else "https://github.com/" + s
    if u.endswith("/"):
        u = u[:-1]
    if u.endswith(".git"):
        u = u[:-4]
    if not u.startswith("https://github.com/"):
        return None
    r = u[len("https://github.com/"):]
    return r if REPO_RE.match(r) and ".." not in r else None


def repo_problem(s):
    """Why s can't be added to a project's repos, or None."""
    if repo_arg(s) is None:
        return "%r isn't a GitHub repo: owner/repo or https://github.com/owner/repo" % s[:60]
    return None


def docker_endpoint(env):
    """(unix socket path, None) for the Docker daemon boxen uses, or (None,
    why) when that isn't a unix socket the stats code can speak to. The same
    rule as bin/boxen: BOXEN_DOCKER_HOST (tests), else your rootless daemon's
    socket in $XDG_RUNTIME_DIR. DOCKER_HOST and docker contexts are ignored,
    as boxen ignores them: the system daemon is never asked."""
    host = env.get("BOXEN_DOCKER_HOST") or ""
    if host:
        if host.startswith("unix://"):
            return host[len("unix://"):], None
        return None, "unsupported endpoint (BOXEN_DOCKER_HOST)"
    run = env.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    return os.path.join(run, "docker.sock"), None


@dataclass
class Config:
    repo: str
    boxen: str
    home: str
    boxen_home: str
    src_root: str
    docker_sock: object     # the Engine API's unix socket, or None (see docker_problem)
    docker_problem: object  # why the Docker endpoint can't be used, or None
    tmux: str
    tmux_socket: str
    tmux_session: str
    state_dir: str

    @property
    def projects_dir(self):
        return os.path.join(self.boxen_home, "projects")

    def proj_dir(self, name):
        return os.path.join(self.projects_dir, name)

    @property
    def global_lock(self):
        """Exists while a global command boxen ui started in a terminal
        (self-update) runs."""
        return os.path.join(self.state_dir, "global.lock")

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        home = os.path.expanduser("~")
        repo = env.get("BOXEN_REPO") or REPO_DIR
        boxen_home = env.get("BOXEN_HOME") or os.path.join(
            env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "boxen")
        src = os.path.realpath(env.get("BOXEN_SRC") or os.path.join(home, "src"))
        sock, problem = docker_endpoint(env)
        state = os.path.join(env.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state"),
                             "boxen", "ui")
        return cls(repo=repo,
                   boxen=env.get("BOXEN_UI_BOXEN") or os.path.join(repo, "bin", "boxen"),
                   home=home, boxen_home=boxen_home, src_root=src, docker_sock=sock, docker_problem=problem,
                   tmux=env.get("BOXEN_UI_TMUX") or "tmux", tmux_socket="boxen",
                   tmux_session="boxen", state_dir=state)
