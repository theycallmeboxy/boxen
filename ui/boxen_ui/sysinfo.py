"""The VM at a glance, for the header. Blocking; called from a thread."""
import os
import socket
import subprocess


def load():
    try:
        return os.getloadavg()
    except OSError:
        return None


def memory():
    """(used, total) bytes from /proc/meminfo (used = total - available)."""
    vals = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                if k in ("MemTotal", "MemAvailable"):
                    vals[k] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    if "MemTotal" not in vals or "MemAvailable" not in vals:
        return None
    return vals["MemTotal"] - vals["MemAvailable"], vals["MemTotal"]


def disk_free(path):
    """(free bytes, the path actually measured, its device) for path, or
    for its nearest readable parent (Docker's root dir is usually root-only,
    and is on the same filesystem as its parent unless mounted apart)."""
    p = path
    while True:
        try:
            st = os.statvfs(p)
            return st.f_bavail * st.f_frsize, p, os.stat(p).st_dev
        except OSError:
            if p in ("/", ""):
                return None
            p = os.path.dirname(p.rstrip("/")) or "/"


def agent_up(sock_path):
    """Whether something accepts connections on the boxen agent's socket.
    Connects and closes without sending anything (boxen-agentd treats that
    as a probe: no reply, not logged)."""
    path = sock_path
    dfd = None
    try:
        if len(path.encode()) > 100:
            # Too long for sockaddr_un: go through the folder's descriptor.
            dfd = os.open(os.path.dirname(path), os.O_PATH | os.O_DIRECTORY)
            path = "/proc/self/fd/%d/%s" % (dfd, os.path.basename(path))
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        try:
            s.connect(path)
            return True
        finally:
            s.close()
    except OSError:
        return False
    finally:
        if dfd is not None:
            os.close(dfd)


def git_hash(repo):
    try:
        out = subprocess.run(["git", "-C", repo, "rev-parse", "--short", "HEAD"],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def human_bytes(n):
    if n is None:
        return "?"
    for unit, size in (("T", 1 << 40), ("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if n >= size:
            v = n / size
            return ("%.1f%s" % (v, unit)) if v < 10 else ("%d%s" % (v, unit))
    return "%dB" % n
