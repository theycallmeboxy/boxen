"""`boxen ls --json`, read defensively, and the short forms the list shows."""
import calendar
import json
import re
import time
from dataclasses import dataclass, field


# Colors, one meaning each: red = error (failed, crashing, exited after
# restarts); amber = needs you (a question, an approval, a token to fix);
# grey = stopped or idle; green = fine.
RED, AMBER, GREY, GREEN = "bold #e5484d", "bold #f5a524", "#8b8d98", "#46a758"
RUNNING_JOB = "bold #3e9bdc"


@dataclass
class Sidecar:
    service: str
    container: str
    image: str


@dataclass
class Container:
    status: str
    restarts: int
    started_at: str
    id: str


@dataclass
class Project:
    name: str
    container: object    # Container or None (not created)
    server: str
    github: str
    repo: object
    sidecars: list = field(default_factory=list)
    supervisor: bool = False

    @property
    def running(self):
        return self.container is not None and self.container.status == "running"


def _s(v, default=""):
    return v if isinstance(v, str) else default


def _project(d, supervisor=False):
    if not isinstance(d, dict) or not isinstance(d.get("name"), str) or not d["name"]:
        return None
    c = d.get("container")
    cont = None
    if isinstance(c, dict):
        r = c.get("restarts")
        cont = Container(_s(c.get("status"), "?"), r if isinstance(r, int) and not isinstance(r, bool) else 0,
                         _s(c.get("started_at")), _s(c.get("id")))
    sidecars = []
    for s in d.get("sidecars") or []:
        if isinstance(s, dict) and isinstance(s.get("service"), str):
            sidecars.append(Sidecar(s["service"], _s(s.get("container")) or "boxen.%s.%s" % (d["name"], s["service"]),
                                    _s(s.get("image"))))
    repo = d.get("repo")
    return Project(d["name"], cont, _s(d.get("server"), "-"), _s(d.get("github"), "-"),
                   repo if isinstance(repo, str) else None, sidecars, supervisor)


def parse_ls(text):
    """(projects, docker reachable) from `boxen ls --json` output, the
    supervisor last. Raises ValueError when it isn't that document."""
    doc = json.loads(text)
    if not isinstance(doc, dict) or not isinstance(doc.get("projects"), list):
        raise ValueError("unexpected output from boxen ls --json")
    out = [p for p in (_project(d) for d in doc["projects"]) if p is not None]
    sup = _project(doc.get("supervisor"), supervisor=True) if doc.get("supervisor") else None
    if sup is not None:
        out.append(sup)
    return out, doc.get("docker") is not False


# Server state (bin/boxen server_state) -> the list's short form.
_SERVER = (("connected", "connected"), ("reconnecting", "reconnecting"), ("starting", "starting"),
           ("waiting", "waiting"), ("not trusted", "not trusted"), ("not signed in", "not signed in"),
           ("not running", "not running"), ("not created", "stopped"))


def server_short(p):
    if not p.running:
        return "stopped"
    for prefix, short in _SERVER:
        if p.server.startswith(prefix):
            return short
    return p.server[:14]


SERVER_STYLE = {"connected": GREEN, "reconnecting": AMBER, "starting": "", "waiting": AMBER,
                "not trusted": AMBER, "not signed in": AMBER, "not running": RED, "stopped": GREY}


def container_short(p):
    if p.container is None:
        return "-"
    return p.container.status


CONTAINER_STYLE = {"running": GREEN, "restarting": RED, "exited": GREY, "dead": RED,
                   "created": GREY, "paused": AMBER, "removing": GREY, "-": GREY}


def container_style(p):
    c = p.container
    if c is None:
        return GREY
    if c.status == "exited" and c.restarts:
        return RED
    if c.status == "running" and c.restarts:
        return AMBER
    return CONTAINER_STYLE.get(c.status, "")


HEALTH_STYLE = {"healthy": GREEN, "unhealthy": RED, "starting": GREY}

_GITHUB = {"token ok": ("ok", GREEN), "token stale": ("stale", AMBER), "no token": ("none", AMBER),
           "run boxen github": ("setup", AMBER), "-": ("-", GREY)}


def github_short(p):
    return _GITHUB.get(p.github, (p.github[:8], ""))


_ISO = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)")


def started_epoch(iso):
    m = _ISO.match(iso or "")
    if not m or iso.startswith("0001"):
        return None
    return calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0))


def uptime(iso, now=None):
    t = started_epoch(iso)
    if t is None:
        return None
    s = max(0, int((now or time.time()) - t))
    if s < 3600:
        return "%dm" % (s // 60)
    if s < 86400:
        return "%dh %02dm" % (s // 3600, (s % 3600) // 60)
    return "%dd %dh" % (s // 86400, (s % 86400) // 3600)
