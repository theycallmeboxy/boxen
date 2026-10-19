"""The `boxen` tmux server (tmux -L boxen): the dashboard's own session and
the windows in it. bin/boxen names a project's Claude sessions "<project>"
(boxen claude; the dashboard's c too) and its Remote Control server
"<project>~serve"; the dashboard's other windows are "<project>/<kind>"
("+global/<kind>" for the rest). "~", "/" and "+" can't be in a project
name, so no project can pass for another or for the dashboard. Windows are
found by name but always addressed by id (@N). They start in your home
folder (-c) unless they're meant to work in the project's folder (Claude,
a VM shell there)."""
import os
import shlex
from dataclasses import dataclass

from . import procs

GLOBAL = "+global"

# After the command: mark the window done (so opening it again starts a
# fresh one), show the exit status, wait for Enter. Ctrl-C stops the
# command but not this: the trap keeps the shell going to the hold.
HOLD = ('tmux set-option -w -t "$TMUX_PANE" @boxen_done 1 2>/dev/null; '
        'printf "\\n[exited %s — press Enter]" "$rc"; read -r _')


@dataclass
class Window:
    id: str
    index: str
    name: str
    active: bool
    dead: bool
    done: bool = False

    @property
    def finished(self):
        """Its command has exited (held for Enter, or the pane is dead)."""
        return self.dead or self.done


def window_name(project, kind):
    return "%s/%s" % (project or GLOBAL, kind)


def serve_window(project):
    return "%s~serve" % project


def kind_of(project, name):
    """What a window is to project: "claude" (a session, named like the
    project), "serve", the dashboard's kind ("shell", "edit-Dockerfile"...),
    or None when it isn't the project's."""
    if name == project:
        return "claude"
    if name == serve_window(project):
        return "serve"
    if name.startswith(project + "/"):
        return name[len(project) + 1:]
    return None


def hold_script(argv, lock=None):
    """The sh script a window runs: argv (each word quoted), then its exit
    status and a wait for Enter, so the last output stays on screen. With
    lock, that file exists while argv runs."""
    cmd = shlex.join(argv)
    if lock:
        q = shlex.quote(lock)
        cmd = "touch %s; %s; rc=$?; rm -f %s" % (q, cmd, q)
    else:
        cmd = "%s; rc=$?" % cmd
    return "trap : INT; %s; %s" % (cmd, HOLD)


def in_boxen_tmux(env=None):
    """True when this process runs inside the boxen tmux server."""
    env = os.environ if env is None else env
    t = env.get("TMUX") or ""
    return bool(t) and os.path.basename(t.split(",", 1)[0]) == "boxen"


class Tmux:
    def __init__(self, exe="tmux", socket="boxen", session="boxen", env=None, home=None):
        self.exe = exe
        self.socket = socket
        self.session = session
        self.env = env
        self.home = home or os.path.expanduser("~")
        self.attached = True          # whether any client is attached (from list_windows)

    def argv(self, *args):
        return [self.exe, "-L", self.socket] + list(args)

    async def run(self, *args, timeout=10.0):
        return await procs.run(self.argv(*args), env=self.env, timeout=timeout)

    async def list_windows(self):
        rc, out, _err = await self.run(
            "list-windows", "-t", self.session + ":",
            "-F", "#{window_id}\t#{window_index}\t#{session_attached}\t#{window_active}\t#{pane_dead}"
                  "\t#{@boxen_done}\t#{window_name}")
        if rc != 0:
            return []
        wins = []
        attached = None
        for line in out.split("\n"):
            f = line.split("\t", 6)
            if len(f) != 7 or not f[0].startswith("@"):
                continue
            attached = f[2] not in ("", "0")
            wins.append(Window(f[0], f[1], f[6], f[3] == "1", f[4] == "1", f[5] == "1"))
        if attached is not None:
            self.attached = attached
        return wins

    async def find(self, name):
        for w in await self.list_windows():
            if w.name == name:
                return w
        return None

    async def select(self, win_id):
        rc, _out, err = await self.run("select-window", "-t", win_id)
        return rc == 0, err.strip()

    async def find_last(self, name):
        """The last window called name whose command still runs, or None."""
        found = None
        for w in await self.list_windows():
            if w.name == name and not w.finished:
                found = w
        return found

    async def open(self, name, argv, lock=None, cwd=None, new=False):
        """Select the window called name if its command still runs, else
        (closing a finished one) start one running argv in cwd (default:
        home), held open after it exits. With new, always start another one
        (Claude sessions: a project can have several). Returns (ok,
        "selected"|"opened", error)."""
        if not new:
            w = await self.find(name)
            if w is not None and not w.finished:
                ok, err = await self.select(w.id)
                return ok, "selected", err
            if w is not None:
                await self.run("kill-window", "-t", w.id)
        rc, _out, err = await self.run(
            "new-window", "-t", self.session + ":", "-c", cwd or self.home, "-n", name,
            "sh", "-c", hold_script(argv, lock=lock))
        return rc == 0, "opened", err.strip()

    async def kill_finished(self, name):
        """Close the windows called name whose command has exited (held
        for Enter), so they don't pile up."""
        for w in await self.list_windows():
            if w.name == name and w.finished:
                await self.run("kill-window", "-t", w.id)

    async def kill(self, win_id):
        rc, _out, err = await self.run("kill-window", "-t", win_id)
        return rc == 0, err.strip()

    async def capture(self, win_id):
        rc, out, _err = await self.run("capture-pane", "-p", "-e", "-t", win_id)
        return out if rc == 0 else None

    async def detach(self):
        """Detach the client this dashboard is showing on (only that one)."""
        rc, tty, _err = await self.run("display-message", "-p", "#{client_tty}")
        tty = tty.strip()
        if rc == 0 and tty.startswith("/dev/"):
            rc, _out, err = await self.run("detach-client", "-t", tty)
        else:
            rc, _out, err = await self.run("detach-client")
        return rc == 0, err.strip()
