"""`boxen ls --json` (version 3), read defensively, and the short forms the
list shows."""
import json
import os
from dataclasses import dataclass, field

from .docker_api import health_of

# Colors, one meaning each: red = error (dead, crashing, unhealthy); amber =
# needs you (a serve window that's gone, a token to fix); grey = stopped or
# idle; green = fine.
RED, AMBER, GREY, GREEN = "bold #e5484d", "bold #f5a524", "#8b8d98", "#46a758"
RUNNING_JOB = "bold #3e9bdc"

LS_VERSION = 3


@dataclass
class Container:
    state: str           # Docker's: running, exited, created, restarting, paused, dead
    status: str          # Docker's words: "Up 2 hours (healthy)", "Exited (0) 3 minutes ago"


@dataclass
class Sidecar:
    service: str
    container: str
    state: str
    status: str


@dataclass
class Repo:
    repo: str            # owner/repo, from the project's list (untrusted text all the same)
    state: str           # ok, not-installed, no-access, error, - (not checked yet)
    origin: bool = False


@dataclass
class Project:
    name: str
    path: str            # the project folder, ~/src/<name>
    container: object    # the build container: Container, or None (not created yet)
    sessions: int        # Claude Code processes running in the folder
    serve: str           # off, on, stopped (on, but its window isn't running)
    github: str
    repo: object
    sidecars: list = field(default_factory=list)
    repos: list = field(default_factory=list)    # Repo, the first one first

    @property
    def env_container(self):
        return "boxen.%s.env" % self.name

    @property
    def running(self):
        return self.container is not None and self.container.state == "running"


def _s(v, default=""):
    return v if isinstance(v, str) else default


def _project(d, src):
    if not isinstance(d, dict) or not isinstance(d.get("name"), str) or not d["name"]:
        return None
    name = d["name"]
    c = d.get("container")
    cont = Container(_s(c.get("state"), "?") or "?", _s(c.get("status"))) if isinstance(c, dict) else None
    sidecars = []
    for s in d.get("sidecars") or []:
        if isinstance(s, dict) and isinstance(s.get("service"), str) and s["service"]:
            sidecars.append(Sidecar(s["service"], _s(s.get("container")) or "boxen.%s.%s" % (name, s["service"]),
                                    _s(s.get("state"), "?") or "?", _s(s.get("status"))))
    n = d.get("sessions")
    sessions = n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0
    serve = d.get("serve") if d.get("serve") in ("off", "on", "stopped") else "off"
    path = d.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        path = os.path.join(src, name)
    repo = d.get("repo")
    repo = repo if isinstance(repo, str) else None
    repos = []
    for r in d.get("repos") if isinstance(d.get("repos"), list) else []:
        if isinstance(r, dict) and isinstance(r.get("repo"), str) and r["repo"]:
            repos.append(Repo(r["repo"], _s(r.get("state"), "-") or "-", r.get("origin") is True))
    return Project(name, path, cont, sessions, serve, _s(d.get("github"), "-"), repo, sidecars, repos)


@dataclass
class Listing:
    projects: list
    docker: bool         # boxen could reach rootless Docker
    v1: list             # boxen v1 projects not migrated yet (names; untrusted text)


def parse_ls(text, src_root):
    """A Listing from `boxen ls --json` output. Raises ValueError when it
    isn't that document (or another version of it: the dashboard and boxen
    come from the same checkout, so that means the dashboard is older or
    newer than boxen and should be restarted)."""
    doc = json.loads(text)
    if not isinstance(doc, dict) or not isinstance(doc.get("projects"), list):
        raise ValueError("unexpected output from boxen ls --json")
    if doc.get("version") != LS_VERSION:
        raise ValueError("boxen ls --json is version %s; this dashboard reads %d: restart it (boxen ui --restart)"
                         % (str(doc.get("version"))[:10], LS_VERSION))
    src = _s(doc.get("src")) or src_root
    projects = [p for p in (_project(d, src) for d in doc["projects"]) if p is not None]
    v1 = [n for n in doc.get("v1") or [] if isinstance(n, str) and n] if isinstance(doc.get("v1"), list) else []
    return Listing(projects, doc.get("docker") is not False, v1)


STATE_STYLE = {"running": GREEN, "restarting": RED, "exited": GREY, "dead": RED,
               "created": GREY, "paused": AMBER, "removing": GREY}
HEALTH_STYLE = {"healthy": GREEN, "unhealthy": RED, "starting": GREY}


def state_style(state, health=None):
    """The style for a container's state (and health, from its status)."""
    if state == "running" and health == "unhealthy":
        return AMBER
    return STATE_STYLE.get(state, "")


def container_short(p):
    return p.container.state if p.container is not None else "-"


def container_style(p):
    c = p.container
    if c is None:
        return GREY
    return state_style(c.state, health_of(c.status))


_SERVE = {"on": ("on", GREEN), "stopped": ("stopped", AMBER), "off": ("-", GREY)}


def serve_short(p):
    return _SERVE.get(p.serve, ("?", ""))


_GITHUB = {"token ok": ("ok", GREEN), "token partial": ("partial", AMBER), "token stale": ("stale", AMBER),
           "no token": ("none", AMBER), "run boxen github": ("setup", AMBER), "-": ("-", GREY)}


def github_short(p):
    return _GITHUB.get(p.github, (p.github[:8], ""))


# A listed repo's state (bin/boxen state.json) -> what the Repos table says.
_REPO_STATE = {"ok": ("ok", GREEN), "not-installed": ("not your repo", GREY), "no-access": ("no access", AMBER),
               "error": ("no token right now", RED), "-": ("not checked yet", GREY)}


def repo_state(state):
    """(label, style) for a repo's state; an unknown one is shown as it is,
    cut short (untrusted: callers clean it like any other text)."""
    return _REPO_STATE.get(state, (state[:20], ""))
