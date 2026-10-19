"""The Docker Engine API over its unix socket, stdlib only, read-only:
ping, info and one-shot container stats. Blocking; called from a thread."""
import http.client
import json
import socket
from urllib.parse import quote


class DockerError(Exception):
    pass


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self._path)
        except OSError:
            s.close()
            raise
        self.sock = s


class DockerAPI:
    def __init__(self, sock_path, timeout=3.0):
        self.sock_path = sock_path
        self.timeout = timeout

    def _get(self, path):
        conn = _UnixConnection(self.sock_path, self.timeout)
        try:
            conn.request("GET", path, headers={"Host": "docker"})
            resp = conn.getresponse()
            body = resp.read(8 * 1024 * 1024)
            if resp.status != 200:
                raise DockerError("%s: HTTP %d" % (path.split("?")[0], resp.status))
            return body
        except (OSError, http.client.HTTPException) as e:
            raise DockerError(str(e) or e.__class__.__name__) from None
        finally:
            conn.close()

    def ping(self):
        try:
            return self._get("/_ping").strip() == b"OK"
        except DockerError:
            return False

    def info(self):
        return json.loads(self._get("/info"))

    def containers(self):
        """Every boxen container (projects' and sidecars'), running or not:
        {name: (state, health or None)}. One call."""
        q = quote(json.dumps({"name": ["boxen"]}), safe="")
        out = {}
        for c in json.loads(self._get("/containers/json?all=1&filters=" + q)):
            if not isinstance(c, dict):
                continue
            for n in c.get("Names") or []:
                if isinstance(n, str):
                    out[n.lstrip("/")] = (str(c.get("State") or "?"), health_of(str(c.get("Status") or "")))
        return out

    def stats(self, container):
        """One stats sample for a container (name or id)."""
        return json.loads(self._get("/containers/%s/stats?stream=false&one-shot=true"
                                    % quote(container, safe="")))


def health_of(status):
    """The health in a container's Status text ("Up 2 hours (healthy)")."""
    for h in ("unhealthy", "healthy"):
        if "(%s)" % h in status:
            return h
    return "starting" if "(health: starting)" in status else None


def _num(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d if isinstance(d, (int, float)) and not isinstance(d, bool) else None


def cpu_percent(prev, cur):
    """CPU use between two samples, as `docker stats` computes it (percent
    of one CPU, so up to 100 x CPUs). A one-shot sample has no precpu
    figures, so the previous sample stands in; None until there is one."""
    base = prev if prev is not None else (cur.get("precpu_stats") and {"cpu_stats": cur["precpu_stats"]})
    if not base:
        return None
    total = _num(cur, "cpu_stats", "cpu_usage", "total_usage")
    system = _num(cur, "cpu_stats", "system_cpu_usage")
    total0 = _num(base, "cpu_stats", "cpu_usage", "total_usage")
    system0 = _num(base, "cpu_stats", "system_cpu_usage")
    if None in (total, system, total0, system0):
        return None
    dt, ds = total - total0, system - system0
    if ds <= 0 or dt < 0:
        return None
    ncpu = _num(cur, "cpu_stats", "online_cpus")
    if not ncpu:
        per = (cur.get("cpu_stats") or {}).get("cpu_usage", {}).get("percpu_usage")
        ncpu = len(per) if isinstance(per, list) and per else 1
    return dt / ds * ncpu * 100.0


def memory(cur):
    """(used, limit) in bytes, as `docker stats` shows them: usage minus
    the inactive page cache. None where it isn't known."""
    usage = _num(cur, "memory_stats", "usage")
    if usage is None:
        return None, None
    stats = (cur.get("memory_stats") or {}).get("stats") or {}
    cache = _num({"s": stats}, "s", "inactive_file")
    if cache is None:
        cache = _num({"s": stats}, "s", "total_inactive_file")
    used = usage - cache if cache is not None and cache < usage else usage
    return used, _num(cur, "memory_stats", "limit")
