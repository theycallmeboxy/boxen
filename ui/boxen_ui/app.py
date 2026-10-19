"""boxen ui: the dashboard.

Data comes in off the UI thread, on a one-second tick that decides what's
due: `boxen ls --json` (a subprocess) every 5 s while someone is looking
(a client attached to the tmux session and the terminal focused), else
every 60 s, and at once after every job and on ctrl+r; container states
and stats from the Docker Engine API (your rootless daemon) every 3 s while
a client is attached (a thread); the VM summary every 5 s (a thread); tmux
windows every second on the Sessions tab, else every 3 s. Actions run
`boxen` as jobs (see jobs.py) or open tmux windows (tmux.py): Claude
sessions in the project's folder, shells, the editor. Nothing from outside
is ever rendered as markup: see textutil.py.
"""
import asyncio
import collections
import json
import os
import shlex
import shutil
import signal
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Label, Log, Select, Sparkline, Static, TabbedContent, TabPane
from textual.worker import NoActiveWorker, get_current_worker

from . import edit, files, procs, sysinfo
from .config import PKG_DIR, Config, repo_arg, valid_name
from .docker_api import DockerAPI, DockerError, cpu_percent, health_of, memory
from .jobs import FAILED, OK, RUNNING, STOPPED, Busy, JobManager, fmt_elapsed
from .jobs import GRACE as JOB_GRACE
from .model import (AMBER, GREEN, GREY, HEALTH_STYLE, RED, RUNNING_JOB, container_short, container_style,
                    github_short, parse_ls, repo_state, serve_short, sessions_short, state_style)
from .screens import ConfirmScreen, GlobalMenuScreen, HelpScreen, NewProjectScreen, ReposScreen
from .textutil import ansi_text, clean_line, text
from .tmux import GLOBAL, Tmux, in_boxen_tmux, kind_of, serve_window, window_name

UI_DIR = os.path.dirname(PKG_DIR)
HIST = 60
HIST_FORGET = 300           # drop a container's series after it's been gone this long
JOB_STYLE = {RUNNING: RUNNING_JOB, OK: GREEN, FAILED: RED, STOPPED: GREY}
LS_ACTIVE, LS_IDLE, STATS_EVERY, SYS_EVERY = 5, 60, 3, 5
DUE_SLACK = 0.05
SUGGEST_EVERY = 30          # `boxen repos NAME --suggest` for the selected project, at most this often
# A Claude session, as `boxen claude` starts one. The tmux server was started
# without DOCKER_HOST (bin/boxen), and this makes sure of it: nothing an
# agent runs gets a way to the Docker daemon.
CLAUDE_ARGV = ["env", "-u", "DOCKER_HOST", "claude"]
# A shell on the VM in a project's folder, which its agent writes: no
# Docker either, and git there doesn't start the fsmonitor program a
# repo's config names (a git-aware prompt would run it, unsandboxed). Its
# hooks still run when you commit.
VM_SHELL_ENV = ["env", "-u", "DOCKER_HOST", "GIT_CONFIG_PARAMETERS='core.fsmonitor='"]

PROJECT_ACTIONS = [
    # (action, palette title, description)
    ("claude", "Claude", "switch to the project's Claude session, or start one"),
    ("claude_new", "New Claude session", "another Claude session in the project folder"),
    ("serve", "Serve on/off", "a Remote Control server for the project, to start sessions from the Claude app"),
    ("up", "Up", "build and start the build container and sidecars, applying .boxen/ changes"),
    ("down", "Down", "stop the build container and sidecars (asks first)"),
    ("shell", "Build shell", "a root shell in the build container, in a terminal window"),
    ("vm_shell", "VM shell", "a shell on the VM in the project folder, in a terminal window"),
    ("sessions_tab", "Sessions", "the project's windows: Claude sessions, serve, shells, with a live preview"),
    ("logs_tab", "Logs", "the sidecars' output"),
    ("files_tab", "Files", "the build environment, your override and the repos list: edit one in vim"),
    ("repos", "GitHub repos", "the repos this project's tokens cover: add, remove"),
    ("repo_add_found", "Add found repo", "add the repo found in the project folder, selected in Overview (asks)"),
    ("rm", "Remove", "remove the project from boxen: build container, image, data, settings (asks; keeps the code)"),
]
GLOBAL_PALETTE = [
    ("new", "New project", "create a project, optionally cloning a GitHub repo"),
    ("global_update_all", "Update everything", "rebuild the base image and every project's build container"),
    ("global_self_update", "Self-update boxen", "pull the latest boxen and apply it (terminal)"),
    ("global_tokens", "Refresh GitHub tokens", "mint fresh tokens for every project"),
    ("global_github", "Connect GitHub", "GitHub App and commit identity (terminal)"),
    ("global_doctor", "Check this VM", "boxen doctor: rootless Docker, the sandbox and isolation"),
    ("global_migrate", "Move from boxen v1", "bring v1 projects over (terminal)"),
    ("refresh", "Refresh", "look at everything again now"),
    ("toggle_jobs", "Jobs panel", "show or hide the jobs panel"),
    ("restart_dashboard", "Restart dashboard", "start the dashboard again (after a boxen update)"),
    ("help", "Keys", "every key boxen ui knows"),
    ("detach", "Detach", "leave boxen ui running; `boxen ui` comes back"),
    ("quit_all", "Quit dashboard", "stop the dashboard and its jobs; sessions and windows keep running"),
]
GETTING_STARTED = """No projects yet.

  n   new project: a name, a GitHub repo (optional)
  g   setup: GitHub, check this VM (doctor),
      and "Move from boxen v1"

More in the README, in
%s"""
WINDOW_WHAT = {"claude": "Claude session", "serve": "Remote Control server", "shell": "build container shell",
               "vm-shell": "VM shell"}


def editor_argv(env=None):
    """$EDITOR if set (it may carry arguments), else vim, else vi."""
    env = os.environ if env is None else env
    ed = (env.get("EDITOR") or "").strip()
    if ed:
        try:
            argv = shlex.split(ed)
        except ValueError:
            argv = []
        if argv:
            return argv
    return ["vim"] if shutil.which("vim", path=env.get("PATH")) else ["vi"]


def window_what(kind):
    if kind in WINDOW_WHAT:
        return WINDOW_WHAT[kind]
    if kind.startswith("edit-"):
        return "editing " + kind[len("edit-"):]
    if kind.startswith("logs-"):
        return "log: " + kind[len("logs-"):]
    return kind


def _plural(n, word, words=None):
    return "%d %s" % (n, word if n == 1 else (words or word + "s"))


def _serve_sessions_note(p):
    """What else stops with a project's serve window."""
    msg = "Sessions started from the Claude app through it stop with it"
    if p.app_sessions:
        msg += " (%d running now)" % p.app_sessions
    return msg + "."


def _fmt_cpu(v):
    if v is None:
        return Text("-", style=GREY)
    return Text(("%.1f%%" % v) if v < 10 else ("%d%%" % round(v)))


def _fmt_mem(v):
    return Text("-", style=GREY) if v is None else Text(sysinfo.human_bytes(v))


class ProjectTable(DataTable):
    BINDINGS = [Binding("j", "cursor_down", "Down", show=False),
                Binding("k", "cursor_up", "Up", show=False)]


class Dashboard(Screen):
    DEFAULT_CSS = """
    Dashboard { layout: vertical; }
    #header { height: 2; background: $boost; padding: 0 1; }
    #header.error { height: 3; }
    #banner { height: auto; background: $warning 30%; padding: 0 1; display: none; }
    #banner.shown { display: block; }
    #main { height: 1fr; }
    #left { width: auto; min-width: 34; max-width: 56%; border-right: tall $panel; }
    #projects { height: 1fr; width: auto; }
    #right { width: 1fr; }
    #proj-title { height: 1; padding: 0 1; }
    #tabs { height: 1fr; }
    #tabs > ContentSwitcher { height: 1fr; }
    TabPane { padding: 0 1; height: 1fr; }
    #getting-started { padding: 1 1; display: none; }
    .empty #getting-started { display: block; }
    .empty #ov-content { display: none; }
    #ov-grid { grid-size: 2; grid-columns: 11 1fr; grid-rows: 1; height: auto; }
    #ov-grid .k { text-style: bold; }
    #ov-grid .v { width: 1fr; text-wrap: nowrap; text-overflow: ellipsis; }
    .section { text-style: bold; color: $accent; margin-top: 1; height: 1; }
    .spark-row { height: 1; margin-top: 1; }
    .spark-row Label { width: 11; text-style: bold; }
    .spark-row Sparkline { width: 1fr; height: 1; }
    .spark-row .now { width: 18; padding-left: 1; }
    #sidecars { height: auto; max-height: 10; }
    #repos { height: auto; max-height: 8; }
    #logs-status, #win-status, #files-note { height: auto; color: $text-muted; }
    #logs-bar { height: 3; }
    #log-source { width: 30; }
    #logs-bar Static { padding: 1 0 0 2; color: $text-muted; }
    #log-view { height: 1fr; }
    #win-list { height: auto; max-height: 9; }
    #win-preview { height: 1fr; border: round $panel; padding: 0 1; overflow: hidden; }
    #files { height: auto; }
    #jobs { height: auto; }
    #jobs-bar { height: 1; background: $panel; padding: 0 1; }
    #jobs-body { height: 11; }
    #jobs.collapsed #jobs-body { display: none; }
    #job-table { width: 40%; max-width: 56; }
    #job-log { width: 1fr; border-left: tall $panel; }
    """

    # ? and q first: on a narrow terminal the footer drops keys from the end.
    BINDINGS = [
        Binding("question_mark", "help", "Keys"),
        Binding("q", "detach", "Detach"),
        Binding("c", "claude", "Claude"),
        Binding("C", "claude_new", "New session", show=False),
        Binding("S", "serve", "Serve"),
        Binding("u", "up", "Up"),
        Binding("d", "down", "Down"),
        Binding("s", "shell", "Shell"),
        Binding("v", "vm_shell", "VM shell", show=False),
        Binding("n", "new", "New"),
        Binding("g", "global_menu", "Global"),
        Binding("J", "toggle_jobs", "Jobs"),
        Binding("f", "follow_logs", "Follow"),
        Binding("x", "window_close", "Close"),
        Binding("a", "repo_add_found", "Add repo"),
        Binding("G", "repos", "Repos"),
        Binding("e", "edit_file", "Edit"),
        Binding("ctrl+r,f5", "refresh", "Refresh", show=False),
        Binding("l", "logs_tab", "Logs", show=False),
        Binding("X", "rm", "Remove", show=False),
        Binding("Q", "quit_all", "Quit", show=False),
    ]
    # Keys that only mean something on one tab: shown (and live) only there.
    TAB_ACTIONS = {"follow_logs": ("logs",), "window_close": ("sessions-tab",), "edit_file": ("files-tab",),
                   "repo_add_found": ("overview",)}

    def __init__(self, cfg, env):
        super().__init__()
        self.cfg = cfg
        self.env = env
        self.tmux = Tmux(cfg.tmux, cfg.tmux_socket, cfg.tmux_session, env=env, home=cfg.home)
        self.jobs = JobManager(cfg.boxen, self, env=env)
        self.jobs.external_global = self._external_global
        self.projects = []
        self.by_name = {}
        self.v1 = []                # boxen v1 projects not migrated yet
        self.selected = None
        self.ls_error = None
        self.ls_loaded = False
        self.ls_docker = True       # what `ls --json` says
        self.sys = {}
        self.docker_ok = None       # owned by the sys thread (a ping each time)
        self.docker_root = None
        self.git = None
        self.git_start = None
        self.live = {}              # container name -> (state, health), from Docker
        self.stats = {}             # container name -> {"cpu", "mem", "limit"}
        self._prev_stats = {}       # container name -> last raw sample
        self.hist = {}              # project -> (cpu deque, mem deque)
        self._hist_seen = {}        # project -> when its container was last running
        self.windows = []
        self._win_rows = None
        self.compact = None
        self.selected_job = None
        self.pending_edits = {}     # result.json path -> edit spec
        self._busy = set()          # refreshes in flight
        self._due = {}              # what -> monotonic time it last ran
        self._ticks = 0
        self._log_source = None
        self.suggest = {}           # project -> (monotonic time, [found repo, ...]) from repos --suggest
        self._repo_rows = []        # Overview's repos table: ("listed" | "found", repo) by row
        self._loaded = {}           # tab -> project it shows
        self._pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="boxen-ui")

    # ------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Static(Text("boxen"), id="header")
        yield Static(Text(""), id="banner")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield ProjectTable(id="projects", cursor_type="row", zebra_stripes=False)
            with Vertical(id="right"):
                yield Static(Text(""), id="proj-title")
                with TabbedContent(id="tabs", initial="overview"):
                    with TabPane("Overview", id="overview"):
                        with VerticalScroll(id="overview-scroll"):
                            yield Static(Text(""), id="getting-started")
                            with Vertical(id="ov-content"):
                                with Grid(id="ov-grid"):
                                    for key in ("container", "sessions", "serve", "folder"):
                                        yield Label(key, classes="k")
                                        yield Static(Text(""), classes="v", id="ov-" + key)
                                with Horizontal(classes="spark-row"):
                                    yield Label("cpu")
                                    yield Sparkline([], id="spark-cpu")
                                    yield Static(Text(""), classes="now", id="cpu-now")
                                with Horizontal(classes="spark-row"):
                                    yield Label("memory")
                                    yield Sparkline([], id="spark-mem")
                                    yield Static(Text(""), classes="now", id="mem-now")
                                yield Static(Text("GitHub repos"), classes="section", id="repos-title")
                                yield DataTable(id="repos", cursor_type="row")
                                yield Static("Sidecars", classes="section")
                                yield DataTable(id="sidecars", cursor_type="none", show_cursor=False)
                    with TabPane("Sessions", id="sessions-tab"):
                        yield Static(Text(""), id="win-status")
                        yield DataTable(id="win-list", cursor_type="row")
                        yield Static(Text(""), id="win-preview")
                    with TabPane("Logs", id="logs"):
                        with Horizontal(id="logs-bar"):
                            yield Select([], id="log-source", allow_blank=True, prompt="sidecar")
                            yield Static(Text("ctrl+r refresh · f follow in a terminal"))
                        yield Static(Text(""), id="logs-status")
                        yield Log(id="log-view", max_lines=2000)
                    with TabPane("Files", id="files-tab"):
                        yield Static(Text("e edits a private copy of the selected file in vim ($EDITOR if set; "
                                          "modelines off), saved back when you quit it. .boxen/ is the "
                                          "project's build environment, which its agent may edit too; the "
                                          "override and repos are yours alone. u applies changes."),
                                     id="files-note")
                        yield DataTable(id="files", cursor_type="row")
        with Vertical(id="jobs", classes="collapsed"):
            yield Static(Text(""), id="jobs-bar")
            with Horizontal(id="jobs-body"):
                yield DataTable(id="job-table", cursor_type="row")
                yield Log(id="job-log", max_lines=3000)
        yield Footer()

    def on_mount(self):
        wl = self.query_one("#win-list", DataTable)
        for label, key in (("WINDOW", "name"), ("WHAT", "what"), ("STATE", "state")):
            wl.add_column(Text(label, style="bold"), key=key)
        fl = self.query_one("#files", DataTable)
        for label, key in (("FILE", "label"), ("PATH", "path"), ("", "state")):
            fl.add_column(Text(label, style="bold"), key=key)
        rt = self.query_one("#repos", DataTable)
        for label, key in (("REPO", "repo"), ("STATE", "state"), ("", "mark")):
            rt.add_column(Text(label, style="bold"), key=key)
        jt = self.query_one("#job-table", DataTable)
        for label, key in (("#", "n"), ("STATUS", "st"), ("TIME", "t"), ("COMMAND", "cmd")):
            jt.add_column(Text(label, style="bold"), key=key)
        self._setup_columns()
        self.query_one("#projects", ProjectTable).focus()
        self._render_header()
        self._render_jobs_bar()
        self._render_selected()
        self.set_interval(1, self.tick)
        self.refresh_ls()
        self.refresh_sys()
        self._refresh_git()

    def on_resize(self, ev):
        self._setup_columns()

    def on_unmount(self):
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------ scheduling

    def looking(self):
        """Someone is looking: a client is attached (or this isn't under
        tmux) and the terminal has focus."""
        return self.tmux.attached and self.app.app_focus

    def _is_due(self, what, every):
        # A little early counts: the one-second ticks jitter by a millisecond
        # either way, and without it a refresh due "every 1 s" skips a tick
        # about one time in three.
        now = time.monotonic()
        if now - self._due.get(what, -1e9) >= every - DUE_SLACK:
            self._due[what] = now
            return True
        return False

    async def tick(self):
        self._ticks += 1
        for j in self.jobs.running():
            self._job_row(j)
        if self.jobs.running():
            self._render_jobs_bar()
        if self._is_due("ls", LS_ACTIVE if self.looking() else LS_IDLE):
            self.refresh_ls()
        if self.tmux.attached and self._is_due("stats", STATS_EVERY):
            self.refresh_stats()
        if self._is_due("sys", SYS_EVERY if self.tmux.attached else LS_IDLE):
            self.refresh_sys()
        if self._is_due("git", 60):
            self._refresh_git()
        self._check_edits()
        # tmux windows: every second on the Sessions tab, else every 3 s
        # (the attach state, the self-update lock, Overview's window count).
        if "tick" in self._busy or (self.active_tab() != "sessions-tab" and self._ticks % 3):
            return
        self._busy.add("tick")
        try:
            await self._refresh_windows()
        finally:
            self._busy.discard("tick")

    def action_refresh(self):
        self._due["ls"] = time.monotonic()
        self.refresh_ls()
        self.refresh_stats()
        self._load_active_tab(force=True)

    # ------------------------------------------------------------ header

    def _render_header(self):
        s = self.sys
        hdr = self.query_one("#header", Static)
        t = Text()
        t.append(" boxen ", style="bold reverse")
        t.append("  %s" % clean_line(socket.gethostname()), style="bold")
        t.append("  ")
        t.append("git %s" % (self.git or "?"), style=GREY)
        t.append("   docker ")
        if self.cfg.docker_problem:
            t.append(self.cfg.docker_problem, style=AMBER)
        elif self.docker_ok is None:
            t.append("?", style=GREY)
        elif self.docker_ok:
            t.append("up", style=GREEN)
        else:
            t.append("down", style=RED)
        t.append("\n")
        ld = s.get("load")
        if ld:
            t.append("load %.2f %.2f %.2f" % ld)
        mem = s.get("mem")
        if mem:
            t.append("   mem %s/%s" % (sysinfo.human_bytes(mem[0]), sysinfo.human_bytes(mem[1])))
        root = s.get("disk_root")
        if root:
            t.append("   / %s free" % sysinfo.human_bytes(root[0]))
        dk = s.get("disk_docker")
        if dk:
            if root and dk[2] == root[2]:
                t.append("   docker: same disk")
            else:
                t.append("   docker %s free" % sysinfo.human_bytes(dk[0]))
        if self.docker_ok is False and s.get("docker_error"):
            t.append("   docker API: %s" % clean_line(s["docker_error"])[:80], style=RED)
        if self.ls_error:
            t.append("\n")
            t.append("boxen ls failed: ", style=RED)
            t.append(clean_line(self.ls_error)[:200], style=RED)
        t.no_wrap = True
        t.overflow = "ellipsis"
        hdr.set_class(bool(self.ls_error), "error")
        hdr.update(t)

    def _render_banner(self):
        b = self.query_one("#banner", Static)
        t = Text()
        if self.git and self.git_start and self.git != self.git_start:
            t.append("boxen was updated ", style="bold")
            t.append("(%s → %s): restart the dashboard to use it: ctrl+p → Restart dashboard, or boxen ui --restart"
                     % (self.git_start, self.git))
        if self.v1:
            if t.plain:
                t.append("\n")
            t.append("boxen v1 projects not moved yet: ", style="bold")
            t.append(clean_line(", ".join(self.v1))[:200])
            t.append(". Run boxen migrate (g → Move from boxen v1).")
        b.update(t)
        b.set_class(bool(t.plain), "shown")

    def _refresh_git(self):
        self._thread(self._git_thread)

    def _git_thread(self):
        h = sysinfo.git_hash(self.cfg.repo)
        self._call(self._set_git, h)

    def _set_git(self, h):
        self.git = h
        if self.git_start is None:
            self.git_start = h
        self._render_header()
        self._render_banner()

    def _thread(self, fn, *args):
        try:
            return self._pool.submit(self._guard, fn, *args)
        except RuntimeError:        # shutting down
            return None

    def _guard(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:      # a refresh must never take the app down
            self._call(self._thread_error, "%s: %s" % (fn.__name__, e))

    def _thread_error(self, msg):
        self.log.error(msg)

    def _call(self, fn, *args):
        """Run fn(*args) on the UI thread (from a worker thread)."""
        try:
            self.app.call_from_thread(fn, *args)
        except Exception:
            pass

    def refresh_sys(self):
        if "sys" in self._busy:
            return
        self._busy.add("sys")
        if self._thread(self._sys_thread) is None:
            self._busy.discard("sys")

    def _sys_thread(self):
        out = {"load": sysinfo.load(), "mem": sysinfo.memory(), "disk_root": sysinfo.disk_free("/")}
        root = self.docker_root
        if self.cfg.docker_sock is None:
            out["docker_ok"] = None
        else:
            # A ping every time: the one place that says whether Docker is up.
            api = DockerAPI(self.cfg.docker_sock, timeout=2.0)
            out["docker_ok"] = api.ping()
            if not out["docker_ok"]:
                out["docker_error"] = "no answer on %s" % self.cfg.docker_sock
            elif root is None:
                try:
                    root = api.info().get("DockerRootDir") or None
                except (DockerError, ValueError, AttributeError):
                    root = None
        if root:
            out["disk_docker"] = sysinfo.disk_free(root)
        out["docker_root"] = root
        self._call(self._sys_done, out)

    def _sys_done(self, out):
        self._busy.discard("sys")
        self.sys = out
        was = self.docker_ok
        self.docker_ok = out.get("docker_ok")
        self.docker_root = out.get("docker_root") or self.docker_root
        if was and not self.docker_ok:
            self.live = {}
            self.stats = {}
            self._render_projects()
            self._render_selected()
        self._render_header()

    # ------------------------------------------------------------ projects

    def refresh_ls(self):
        if "ls" in self._busy:
            return
        self._busy.add("ls")
        self._due["ls"] = time.monotonic()
        self.run_worker(self._ls_worker(), group="ls", exit_on_error=False)

    async def _ls_worker(self):
        try:
            rc, out, err = await procs.run([self.cfg.boxen, "ls", "--json"], env=self.jobs.env, timeout=60)
            if rc != 0:
                lines = [ln for ln in (err or out).strip().splitlines() if ln.strip()]
                self.ls_error = (lines[-1] if lines else "exit %d" % rc)
            else:
                try:
                    ls = parse_ls(out, self.cfg.src_root)
                except ValueError as e:
                    self.ls_error = str(e)
                else:
                    self.ls_error = None
                    first = not self.projects
                    self.projects = ls.projects
                    self.ls_docker = ls.docker
                    self.v1 = ls.v1
                    self.by_name = {p.name: p for p in ls.projects}
                    if first:
                        self.refresh_stats()
            self.ls_loaded = True
            self._render_projects(force=True if self._stale_changed() else False)
            self._render_header()
            self._render_banner()
            self._render_selected()
        finally:
            self._busy.discard("ls")

    def _stale_changed(self):
        stale = bool(self.ls_error)
        changed = getattr(self, "_was_stale", False) != stale
        self._was_stale = stale
        if changed:
            self.compact = None
            self._setup_columns()
        return changed

    def _setup_columns(self):
        compact = self.app.size.width < 110
        if compact == self.compact:
            return
        self.compact = compact
        t = self.query_one("#projects", ProjectTable)
        t.clear(columns=True)
        cols = [("PROJECT (stale)" if self.ls_error else "PROJECT", "name"),
                ("CTR" if compact else "CONTAINER", "ctr"), ("SESS", "sess"), ("SERVE", "serve"),
                ("CPU", "cpu"), ("MEM", "mem")]
        if not compact:
            cols.append(("GITHUB", "gh"))
        for label, key in cols:
            t.add_column(Text(label, style="bold"), key=key)
        sc = self.query_one("#sidecars", DataTable)
        sc.clear(columns=True)
        cols = [("SERVICE", "svc"), ("STATE", "st"), ("HEALTH", "h"), ("CPU", "cpu"), ("MEM", "mem")] + \
               ([] if compact else [("STATUS", "status")])
        for label, key in cols:
            sc.add_column(Text(label, style="bold"), key=key)
        self._render_projects(force=True)
        self._render_sidecars()

    def _env_state(self, p):
        """(state, health) of the build container: as Docker says now, else
        as boxen ls said; None when it doesn't exist."""
        live = self.live.get(p.env_container)
        if live is not None:
            return live
        if p.container is None:
            return None
        return p.container.state, health_of(p.container.status)

    def _env_running(self, p):
        st = self._env_state(p)
        return st is not None and st[0] == "running"

    def _project_cells(self, p):
        stale = bool(self.ls_error)
        name = Text(clean_line(p.name)[:24], style="bold")
        if not self.ls_docker:
            ctr_t = Text("?", style=GREY)
        else:
            st = self._env_state(p)
            ctr_t = (Text(clean_line(st[0]), style=state_style(*st)) if st is not None
                     else Text(container_short(p), style=container_style(p)))
        sess = sessions_short(p)
        sess_t = Text(sess, style=GREY) if sess == "-" else Text(sess, style="bold")
        sv, sv_style = serve_short(p)
        st = self.stats.get(p.env_container, {}) if self._env_running(p) else {}
        cells = [name, ctr_t, sess_t, Text(sv, style=sv_style), _fmt_cpu(st.get("cpu")), _fmt_mem(st.get("mem"))]
        if not self.compact:
            g, style = github_short(p)
            cells.append(Text(clean_line(g), style=style))
        if stale:
            for c in cells:
                c.stylize("dim")
        return cells

    def _render_projects(self, force=False):
        t = self.query_one("#projects", ProjectTable)
        keys = [p.name for p in self.projects]
        have = [k.value for k in t.rows]
        if force or have != keys:
            sel = self.selected
            t.clear()
            for k in keys:
                t.add_row(*self._project_cells(self.by_name[k]), key=k)
            if keys:
                idx = keys.index(sel) if sel in keys else 0
                t.move_cursor(row=idx)
                if keys[idx] != sel:
                    self.select_project(keys[idx])
            else:
                self.select_project(None)
        else:
            colkeys = [c.value for c in t.columns]
            for k in keys:
                for ck, cell in zip(colkeys, self._project_cells(self.by_name[k]), strict=True):
                    t.update_cell(k, ck, cell)

    @on(DataTable.RowHighlighted, "#projects")
    def _project_highlighted(self, ev):
        key = ev.row_key.value if ev.row_key is not None else None
        if key:
            self.select_project(key)

    @on(DataTable.RowSelected, "#projects")
    def _project_selected(self, ev):
        self.query_one("#tabs", TabbedContent).focus()

    def select_project(self, name):
        if name == self.selected:
            return
        self.selected = name
        self._loaded = {}
        self._render_selected()
        self._load_active_tab()

    def current(self):
        return self.by_name.get(self.selected) if self.selected else None

    # ------------------------------------------------------------ overview

    def _render_selected(self):
        p = self.current()
        title = self.query_one("#proj-title", Static)
        gs = self.query_one("#getting-started", Static)
        if p is None:
            if not self.ls_loaded:
                msg = Text("loading…")
            elif self.ls_error and not self.projects:
                msg = Text("boxen ls failed; the header says why. Retrying.")
            else:
                msg = Text(GETTING_STARTED % os.path.join(self.cfg.repo, "README.md"))
            title.update(Text("no project", style=GREY))
            gs.update(msg)
            self.query_one("#overview").add_class("empty")
            return
        self.query_one("#overview").remove_class("empty")
        title.update(Text(clean_line(p.name), style="bold"))

        def val(key, *parts):
            v = Text(no_wrap=True, overflow="ellipsis")
            for part in parts:
                if isinstance(part, Text):
                    v.append_text(part)
                else:
                    v.append(part)
            self.query_one("#ov-" + key, Static).update(v)

        st = self._env_state(p)
        if not self.ls_docker:
            val("container", Text("unknown: rootless Docker isn't answering", style=GREY))
        elif st is None:
            val("container", Text("not created", style=GREY), Text("  u builds and starts it", style=GREY))
        else:
            val("container", Text(clean_line(st[0]), style=state_style(*st)),
                "  %s" % clean_line(p.container.status) if p.container and p.container.status else "",
                Text("  %s" % clean_line(p.env_container), style=GREY))
        self._render_sessions_line()
        if p.serve == "on":
            val("serve", Text("on", style=GREEN), ": start sessions from the Claude app",
                Text("  (window %s)" % clean_line(serve_window(p.name)), style=GREY))
        elif p.serve == "stopped":
            val("serve", Text("stopped", style=AMBER), ": on, but its window isn't running; S starts it again")
        else:
            val("serve", Text("off", style=GREY), Text("  S keeps a Remote Control server up, to start sessions "
                                                     "from the Claude app", style=GREY))
        val("folder", clean_line(p.path))
        self._render_repos()
        self._load_suggest(p)
        self._render_stats()
        self._render_sidecars()

    def _claude_windows(self, name, running=True):
        return [w for w in self.windows if w.name == name and not (running and w.finished)]

    def _render_sessions_line(self):
        p = self.current()
        if p is None:
            return
        v = Text(no_wrap=True, overflow="ellipsis")
        if p.sessions:
            v.append(_plural(p.sessions, "Claude process", "Claude processes") + " in the folder", style="bold")
        if p.app_sessions:
            v.append("%s%d from the app, via serve" % (" + " if p.sessions else "", p.app_sessions), style="bold")
        if not p.sessions and not p.app_sessions:
            v.append("none running", style=GREY)
        nw = len(self._claude_windows(p.name))
        if nw:
            v.append("  %s here (Sessions tab)" % _plural(nw, "window"), style=GREY)
        v.append("  c %s, C starts another" % ("switches to one" if nw else "starts one"), style=GREY)
        self.query_one("#ov-sessions", Static).update(v)

    # GitHub repos (Overview)

    def _found(self, name):
        """The repos found in the project's folder but not listed (from its
        last `boxen repos NAME --suggest`): untrusted text, shown as such."""
        return list(self.suggest.get(name, (0, []))[1])

    def _load_suggest(self, p, force=False):
        if p is None or ("suggest", p.name) in self._busy:
            return
        last = self.suggest.get(p.name)
        if not force and last is not None and time.monotonic() - last[0] < SUGGEST_EVERY:
            return
        self._busy.add(("suggest", p.name))
        self.run_worker(self._suggest_worker(p.name), group="suggest", exit_on_error=False)

    async def _suggest_worker(self, name):
        try:
            rc, out, _err = await procs.run([self.cfg.boxen, "repos", name, "--suggest"], env=self.jobs.env,
                                            timeout=30)
            found = [ln.strip() for ln in out.splitlines() if ln.strip()][:50] if rc == 0 else []
            self.suggest[name] = (time.monotonic(), found)
            if name == self.selected:
                self._render_repos()
        finally:
            self._busy.discard(("suggest", name))

    def _render_repos(self):
        p = self.current()
        t = self.query_one("#repos", DataTable)
        title = self.query_one("#repos-title", Static)
        t.clear()
        self._repo_rows = []
        if p is None:
            return
        _g, gstyle = github_short(p)
        title.update(Text.assemble("GitHub repos  ", (clean_line(p.github), gstyle), ("   G changes them", GREY)))
        for r in p.repos:
            label, style = repo_state(r.state)
            t.add_row(text(r.repo), Text(clean_line(label), style=style), Text("origin" if r.origin else "", style=GREY))
            self._repo_rows.append(("listed", r.repo))
        for f in self._found(p.name):
            t.add_row(text(f), Text("found, not listed", style=AMBER),
                      Text("a adds" if repo_arg(f) else "not a repo name", style=GREY))
            self._repo_rows.append(("found", f))
        if not self._repo_rows:
            t.add_row(Text("none: G adds one", style=GREY), Text(""), Text(""))

    def action_repos(self):
        p = self._need_project()
        if not p:
            return

        def done(res):
            if not res:
                return
            op, repos = res
            if op == "add":
                self.start_job(["repos", p.name, "add"] + repos, project=p.name)
            elif op == "rm":
                self._confirm("Remove %s from %s's repos?" % (", ".join(repos), p.name),
                              "Its tokens stop covering %s at the next refresh (boxen does one now)."
                              % ", ".join(repos),
                              lambda: self.start_job(["repos", p.name, "rm"] + repos, project=p.name),
                              yes="Remove")
        self.app.push_screen(ReposScreen(p.name, p.repos, self._found(p.name)), done)

    def action_repo_add_found(self):
        """a on Overview: add the found repo the Repos table's cursor is on
        (or the only one), after asking: the name comes from the project's
        own git config, which its agent writes."""
        p = self._need_project()
        if not p:
            return
        t = self.query_one("#repos", DataTable)
        found = [r for kind, r in self._repo_rows if kind == "found" and repo_arg(r)]
        pick = None
        i = t.cursor_row
        if 0 <= i < len(self._repo_rows) and self._repo_rows[i][0] == "found" and repo_arg(self._repo_rows[i][1]):
            pick = self._repo_rows[i][1]
        elif len(found) == 1:
            pick = found[0]
        if pick is None:
            self.notify("Select a found repo in the GitHub repos table (or press G)." if found
                        else "No repos found in the project folder that aren't listed.", markup=False)
            return
        repo = repo_arg(pick)
        self._confirm("Let %s push to %s?" % (p.name, repo),
                      "Adds %s to the repos %s's tokens cover: its agent can push there (and use gh on it). "
                      "It was found in the project folder's git remotes, which the agent can change." % (repo, p.name),
                      lambda: self.start_job(["repos", p.name, "add", repo], project=p.name), warn=True, yes="Add")

    def _render_sidecars(self):
        p = self.current()
        t = self.query_one("#sidecars", DataTable)
        t.clear()
        if p is None:
            return
        ncol = len(t.columns)
        if not p.sidecars:
            t.add_row(Text("none", style=GREY), *[Text("")] * (ncol - 1))
            return
        for sc in p.sidecars:
            live = self.live.get(sc.container)
            if live is not None:
                state, health = live
            elif not self.ls_docker:
                state, health = "?", None
            else:
                state, health = sc.state, health_of(sc.status)
            st = self.stats.get(sc.container, {}) if state == "running" else {}
            cells = [Text(clean_line(sc.service), style="bold"),
                     Text(clean_line(state), style=state_style(state, health) or GREY),
                     Text(clean_line(health or "-"), style=HEALTH_STYLE.get(health or "", GREY)),
                     _fmt_cpu(st.get("cpu")), _fmt_mem(st.get("mem"))]
            if not self.compact:
                cells.append(Text(clean_line(sc.status), style=GREY))
            t.add_row(*cells)

    def _render_stats(self):
        p = self.current()
        if p is None:
            return
        cpu_h, mem_h = self.hist.get(p.name, ((), ()))
        self.query_one("#spark-cpu", Sparkline).data = list(cpu_h)
        self.query_one("#spark-mem", Sparkline).data = list(mem_h)
        running = self._env_running(p)
        st = self.stats.get(p.env_container, {}) if running else {}
        self.query_one("#cpu-now", Static).update(_fmt_cpu(st.get("cpu")) if running else Text("stopped", style=GREY))
        m, lim = st.get("mem"), st.get("limit")
        mt = _fmt_mem(m)
        if m is not None and lim:
            mt.append(" / %s" % sysinfo.human_bytes(lim), style=GREY)
        self.query_one("#mem-now", Static).update(mt if running else Text("stopped", style=GREY))

    # ------------------------------------------------------------ stats

    def refresh_stats(self):
        if "stats" in self._busy or self.cfg.docker_sock is None or self.docker_ok is False:
            return
        known = set()
        for p in self.projects:
            known.add(p.env_container)
            known.update(sc.container for sc in p.sidecars)
        self._busy.add("stats")
        self._due["stats"] = time.monotonic()
        if self._thread(self._stats_thread, known) is None:
            self._busy.discard("stats")

    def _stats_thread(self, known):
        api = DockerAPI(self.cfg.docker_sock, timeout=3.0)
        try:
            live = {n: v for n, v in api.containers().items() if n in known}
        except (DockerError, ValueError):
            self._call(self._stats_done, None, None)
            return
        samples = {}

        def one(name):
            try:
                return name, api.stats(name)
            except (DockerError, ValueError):
                return name, None
        running = [n for n, (state, _h) in live.items() if state == "running"]
        if running:
            with ThreadPoolExecutor(max_workers=min(6, len(running))) as ex:
                for name, sample in ex.map(one, running):
                    if isinstance(sample, dict):
                        samples[name] = sample
        self._call(self._stats_done, live, samples)

    def _stats_done(self, live, samples):
        self._busy.discard("stats")
        if live is None:
            self.stats = {}
            return
        self.live = live
        stats = {}
        for name, cur in samples.items():
            used, limit = memory(cur)
            stats[name] = {"cpu": cpu_percent(self._prev_stats.get(name), cur), "mem": used, "limit": limit}
        self._prev_stats = samples
        self.stats = stats
        now = time.monotonic()
        for p in self.projects:
            st = stats.get(p.env_container)
            if not st:
                continue
            self._hist_seen[p.name] = now
            ch, mh = self.hist.setdefault(p.name, (collections.deque(maxlen=HIST), collections.deque(maxlen=HIST)))
            if st["cpu"] is not None:
                ch.append(st["cpu"])
            if st["mem"] is not None:
                mh.append(st["mem"])
        for name in [n for n, seen in self._hist_seen.items() if now - seen > HIST_FORGET]:
            self.hist.pop(name, None)
            self._hist_seen.pop(name, None)
        self._render_projects()
        self._render_stats()
        if self.current() and self.current().sidecars:
            self._render_sidecars()

    # ------------------------------------------------------------ tabs

    def active_tab(self):
        return self.query_one("#tabs", TabbedContent).active

    @on(TabbedContent.TabActivated, "#tabs")
    def _tab_activated(self, ev):
        self.refresh_bindings()
        self._load_active_tab()

    def _load_active_tab(self, force=False):
        tab = self.active_tab()
        p = self.selected
        if not force and self._loaded.get(tab) == p:
            return
        self._loaded[tab] = p
        if tab == "logs":
            self._sync_log_sources()
            self.load_logs()
        elif tab == "sessions-tab":
            self._win_rows = None
            self.run_worker(self._refresh_windows(), group="windows-tab", exclusive=True, exit_on_error=False)
        elif tab == "files-tab":
            self._render_files()

    def check_action(self, action, parameters):
        tabs = self.TAB_ACTIONS.get(action)
        if tabs is not None:
            try:
                return self.active_tab() in tabs
            except Exception:
                return False
        return True

    # sessions: the project's tmux windows

    async def _refresh_windows(self):
        if not in_boxen_tmux(self.env):
            self.windows = []
        else:
            self.windows = await self.tmux.list_windows()
        if self.current() is not None and self.active_tab() == "overview":
            self._render_sessions_line()
        if self.active_tab() != "sessions-tab":
            return
        p = self.selected
        status = self.query_one("#win-status", Static)
        table = self.query_one("#win-list", DataTable)
        preview = self.query_one("#win-preview", Static)
        if not in_boxen_tmux(self.env):
            status.update(Text("Not running inside `boxen ui`'s tmux session: windows are unavailable. "
                               "Start the dashboard with `boxen ui`.", style=AMBER))
            return
        mine = [(w, kind_of(p, w.name)) for w in self.windows] if p else []
        mine = [(w, k) for w, k in mine if k]
        rows = [(w.id, w.name, w.finished) for w, _k in mine]
        if self._win_rows != rows:
            self._win_rows = rows
            cur = table.cursor_row
            table.clear()
            for w, k in mine:
                table.add_row(text(w.name), text(window_what(k)),
                              Text("exited" if w.finished else "running", style=GREY if w.finished else GREEN),
                              key=w.id)
            if mine:
                table.move_cursor(row=min(max(cur, 0), len(mine) - 1))
        proj = self.current()
        if not mine:
            msg = "No windows for this project. c starts a Claude session, s a shell in its build container, " \
                  "v one on the VM in its folder."
            if proj is not None and proj.sessions:
                msg += " (%s run in the folder outside boxen's tmux.)" % _plural(proj.sessions, "Claude process", "Claude processes")
            status.update(Text(msg, style=GREY))
            preview.update(Text(""))
            return
        status.update(Text("Enter switches to the window · x closes it · F1 (or C-b 0) comes back here", style=GREY))
        wid = self._selected_window()
        if wid is None:
            return
        cap = await self.tmux.capture(wid)
        if cap is None or self.active_tab() != "sessions-tab":
            return
        lines = cap.rstrip("\n").split("\n")
        while lines and not lines[-1].strip():
            lines.pop()
        h = max(1, preview.size.height or 10)
        preview.update(ansi_text("\n".join(lines[-h:])))

    def _selected_window(self):
        t = self.query_one("#win-list", DataTable)
        if t.row_count == 0:
            return None
        try:
            return t.coordinate_to_cell_key((t.cursor_row, 0)).row_key.value
        except Exception:
            return None

    @on(DataTable.RowSelected, "#win-list")
    def _window_selected(self, ev):
        wid = ev.row_key.value if ev.row_key is not None else None
        if wid:
            self.run_worker(self._select_window(wid), exit_on_error=False)

    async def _select_window(self, wid):
        ok, err = await self.tmux.select(wid)
        if not ok:
            self.notify("tmux: %s" % (err or "couldn't switch"), severity="error", markup=False)

    def action_window_close(self):
        p, wid = self.current(), self._selected_window()
        if p is None or wid is None:
            return
        w = next((x for x in self.windows if x.id == wid), None)
        kind = kind_of(p.name, w.name) if w else None
        if w is None or kind is None:
            return
        if kind == "claude":
            msg = "Ends that Claude session (its conversation is kept: claude --resume in the folder picks it up)."
        elif kind == "serve":
            msg = ("Stops the Remote Control server until it's started again (S, or the next boot while serve "
                   "is on). %s To turn serve off for good: S." % _serve_sessions_note(p))
        else:
            msg = "Ends what runs in it."
        name = clean_line(w.name)

        async def go():
            ok, err = await self.tmux.kill(wid)
            if not ok:
                self.notify("tmux: %s" % (err or "couldn't close it"), severity="error", markup=False)
            await self._refresh_windows()
            self.refresh_ls()
        self._confirm("Close window %s?" % name, msg, lambda: self.run_worker(go(), exit_on_error=False),
                      warn=kind in ("claude", "serve"), yes="Close")

    # logs: the sidecars'

    def _sync_log_sources(self):
        p = self.current()
        opts = [(Text(clean_line(sc.service)), sc.service) for sc in p.sidecars] if p else []
        sel = self.query_one("#log-source", Select)
        values = [v for _l, v in opts]
        if self._log_source not in values:
            self._log_source = values[0] if values else None
        with sel.prevent(Select.Changed):
            sel.set_options(opts)
            if self._log_source is not None:
                sel.value = self._log_source

    @on(Select.Changed, "#log-source")
    def _log_source_changed(self, ev):
        if ev.value is Select.NULL or ev.value == self._log_source:
            return
        self._log_source = ev.value
        self.load_logs()

    def load_logs(self):
        self.run_worker(self._logs_worker(self.selected, self._log_source), group="logs", exclusive=True,
                        exit_on_error=False)

    def _sidecar(self, p, svc):
        for sc in p.sidecars:
            if sc.service == svc:
                return sc
        return None

    async def _logs_worker(self, name, source):
        view = self.query_one("#log-view", Log)
        status = self.query_one("#logs-status", Static)
        view.clear()
        p = self.by_name.get(name) if name else None
        if p is None:
            status.update(Text("no project selected", style=GREY))
            return
        if source is None or self._sidecar(p, source) is None:
            status.update(Text("No sidecars: .boxen/services.yaml declares none. Background processes have their "
                               "own logs: boxen run --logs NAME, in the project.", style=GREY))
            return
        # boxen env acts on the project of the folder it runs in, and strips
        # control characters from the log.
        argv = [self.cfg.boxen, "env", "logs", source, "--tail", "300"]
        what = "boxen env logs %s --tail 300" % source
        status.update(Text("loading: %s" % clean_line(what), style=GREY))
        rc, out, _err = await procs.run(argv, env=self.jobs.env, timeout=60, merge_stderr=True, cwd=p.path)
        if name != self.selected or source != self._log_source:
            return
        lines = [clean_line(x) for x in out.splitlines()]
        view.write_lines(lines)
        status.update(Text("%s · %d lines%s" % (clean_line(what), len(lines), "" if rc == 0 else " · exit %d" % rc),
                           style=GREY if rc == 0 else RED))

    def action_follow_logs(self):
        p = self.current()
        if not p or self._log_source is None:
            return
        sc = self._sidecar(p, self._log_source)
        if sc is None:
            return
        # boxen env acts on the folder it runs in, and strips control
        # characters, so a sidecar's output can't drive your terminal.
        self.open_window(window_name(p.name, "logs-" + sc.service),
                         [self.cfg.boxen, "env", "logs", sc.service, "-f", "--tail", "100"], cwd=p.path)

    # files

    def _file_entries(self, p):
        """(label, base, path parts below base, kind): kind "env" for the
        agent-editable build environment in the project folder, "override"
        and "repos" for boxen's (yours) in projects/<name>."""
        pdir = self.cfg.proj_dir(p.name)
        return [(".boxen/Dockerfile", p.path, [".boxen", "Dockerfile"], "env"),
                (".boxen/services.yaml", p.path, [".boxen", "services.yaml"], "env"),
                ("compose.override.yaml", pdir, ["compose.override.yaml"], "override"),
                ("repos", pdir, ["repos"], "repos")]

    def _render_files(self):
        t = self.query_one("#files", DataTable)
        t.clear()
        p = self.current()
        if p is None:
            return
        for label, base, rel, kind in self._file_entries(p):
            st = files.state_under(base, rel, limit=edit.LIMIT)
            if st == "file":
                cell = Text("")
            elif st == "missing":
                cell = Text("(new, from the example)" if kind == "override" else "(new)", style=GREY)
            else:
                cell = Text("refused: %s" % clean_line(st), style=RED)
            t.add_row(Text(label, style="bold"), text(os.path.join(base, *rel)), cell, key=label)

    def action_edit_file(self):
        p = self.current()
        t = self.query_one("#files", DataTable)
        if p is None or t.row_count == 0:
            return
        entries = self._file_entries(p)
        entry = entries[min(max(t.cursor_row, 0), len(entries) - 1)]
        if not in_boxen_tmux(self.env):
            self.notify("Editing needs the dashboard to run in `boxen ui` (tmux).", severity="warning",
                        markup=False)
            return
        self.run_worker(self._edit(p.name, *entry), group="windows", exit_on_error=False)

    async def _edit(self, name, label, base, rel, kind):
        wname = window_name(name, "edit-" + rel[-1])
        w = await self.tmux.find(wname)
        if w is not None and not w.finished:
            await self.tmux.select(w.id)
            return
        template = None
        if kind == "override":
            try:
                template = files.read_bytes_under(base, ["compose.override.example.yaml"], limit=edit.LIMIT)
            except files.Refused:
                template = None
        try:
            spec = edit.prepare(name, label, base, rel, editor_argv(self.env), env=self.env, template=template)
        except (files.Refused, OSError) as e:
            self.notify("%s: %s; not opened" % (label, e), severity="error", markup=False)
            return
        spec["kind"] = kind
        argv = ["env", "PYTHONPATH=" + UI_DIR, sys.executable, "-P", "-m", "boxen_ui.edit",
                os.path.join(spec["dir"], "spec.json"), spec["sha"]]
        self.pending_edits[spec["result"]] = spec
        ok, _how, err = await self.tmux.open(wname, argv)
        if not ok:
            self.pending_edits.pop(spec["result"], None)
            shutil.rmtree(spec["dir"], ignore_errors=True)
            self.notify("tmux: %s" % (err or "couldn't open a window"), severity="error", markup=False)

    def _check_edits(self):
        for path, spec in list(self.pending_edits.items()):
            if not os.path.exists(path):
                continue
            del self.pending_edits[path]
            try:
                with open(path) as f:
                    res = json.load(f)
            except (OSError, ValueError):
                res = {"status": "refused", "message": "no result"}
            if not res.get("kept"):
                shutil.rmtree(spec["dir"], ignore_errors=True)
            self._edit_done(spec, res)

    def _edit_done(self, spec, res):
        name, label, status = spec["project"], spec["label"], res.get("status")
        msg = clean_line(str(res.get("message", "")))
        if status == "written" and spec.get("kind") in ("env", "override"):
            self._confirm("Apply %s's build environment now?" % name,
                          "Saved %s. boxen up %s checks .boxen/ and your override, builds, and recreates the "
                          "build container and sidecars where something changed (background processes in it "
                          "stop). Nothing changes if a check fails. Or later: u." % (label, name),
                          lambda: self.start_job(["up", name], project=name), yes="Apply now")
        elif status == "written" and spec.get("kind") == "repos":
            self.notify("%s: saved repos; minting its tokens for them (boxen tokens %s)" % (name, name),
                        markup=False)
            self.start_job(["tokens", name], project=name)
        elif status == "written":
            self.notify("%s: saved %s" % (name, label), markup=False)
        elif status in ("conflict", "refused"):
            self.notify("%s: %s" % (name, msg), severity="error", markup=False, timeout=12)
        if self.active_tab() == "files-tab":
            self._render_files()

    # ------------------------------------------------------------ jobs

    def start_job(self, args, project=None, is_global=False, then=None):
        if project is not None and not valid_name(project):
            self.notify("not a valid project name", severity="error", markup=False)
            return None
        try:
            job = self.jobs.start(args, project=project, is_global=is_global)
        except Busy as e:
            self.notify(str(e), severity="warning", markup=False)
            return None
        job.then = then
        t = self.query_one("#job-table", DataTable)
        for old in self.jobs.pruned:
            try:
                t.remove_row(str(old.id))
            except Exception:
                pass
        self.selected_job = job.id
        self._job_row(job)
        self._show_job(job)
        self._render_jobs_bar()
        self.run_worker(self.jobs.run(job), group="jobs", exit_on_error=False)
        return job

    def _job_cells(self, job):
        return [Text(str(job.id)), Text(job.status, style=JOB_STYLE.get(job.status, "")),
                Text(fmt_elapsed(job.elapsed())), text(job.display)]

    def _job_row(self, job):
        t = self.query_one("#job-table", DataTable)
        key = str(job.id)
        if key in [k.value for k in t.rows]:
            for ck, cell in zip(("n", "st", "t", "cmd"), self._job_cells(job), strict=True):
                t.update_cell(key, ck, cell)
        else:
            t.add_row(*self._job_cells(job), key=key)
            t.move_cursor(row=t.row_count - 1)

    def _show_job(self, job):
        log = self.query_one("#job-log", Log)
        log.clear()
        log.write_line("$ %s" % clean_line(job.title))
        if job.dropped:
            log.write_line("[%d earlier lines not kept]" % job.dropped)
        log.write_lines(job.lines)
        if job.status != RUNNING:
            log.write_line("[%s%s, %s]" % (job.status, "" if job.rc is None else " · exit %d" % job.rc,
                                           fmt_elapsed(job.elapsed())))

    @on(DataTable.RowHighlighted, "#job-table")
    def _job_highlighted(self, ev):
        key = ev.row_key.value if ev.row_key is not None else None
        if key and key != str(self.selected_job):
            job = self._job(int(key))
            if job:
                self.selected_job = job.id
                self._show_job(job)

    def _job(self, jid):
        for j in self.jobs.jobs:
            if j.id == jid:
                return j
        return None

    def _render_jobs_bar(self):
        bar = self.query_one("#jobs-bar", Static)
        collapsed = self.query_one("#jobs").has_class("collapsed")
        t = Text()
        t.append("▸ Jobs " if collapsed else "▾ Jobs ", style="bold")
        js = self.jobs.jobs
        if not js:
            t.append(" none yet · J shows this panel", style=GREY)
        else:
            counts = collections.Counter(j.status for j in js)
            parts = ["%d %s" % (counts[s], s) for s in (RUNNING, FAILED) if counts[s]]
            t.append(" " + (" · ".join(parts) if parts else "all done"), style=GREY)
            last = js[-1]
            t.append("   ")
            t.append(clean_line(last.display), style="bold")
            t.append(" %s %s" % (last.status, fmt_elapsed(last.elapsed())), style=JOB_STYLE.get(last.status, ""))
            if collapsed and last.lines:
                t.append("  " + clean_line(last.lines[-1])[:120], style=GREY)
        t.no_wrap = True
        t.overflow = "ellipsis"
        bar.update(t)

    # JobManager listener

    def job_changed(self, job):
        self._job_row(job)
        self._render_jobs_bar()

    def job_line(self, job, line):
        if job.id == self.selected_job:
            self.query_one("#job-log", Log).write_line(line)
        self._render_jobs_bar()

    def job_done(self, job):
        if job.id == self.selected_job:
            self.query_one("#job-log", Log).write_line(
                "[%s%s, %s]" % (job.status, "" if job.rc is None else " · exit %d" % job.rc, fmt_elapsed(job.elapsed())))
        if job.status == OK:
            self.notify("%s: done" % job.title, markup=False, timeout=4)
            if job.then is not None:
                job.then(job)
        elif job.status == FAILED:
            last = job.lines[-1] if job.lines else "exit %s" % job.rc
            self.notify("%s failed: %s" % (job.title, clean_line(last)[:200]), severity="error", markup=False,
                        timeout=8)
        # The state changed: look again now rather than at the next poll.
        self.call_later(self.refresh_ls)
        if job.project and job.project in self.suggest:
            del self.suggest[job.project]
            if job.project == self.selected:
                self._load_suggest(self.current(), force=True)

    def _external_global(self):
        """A global command in a terminal window (self-update) is running."""
        if os.path.exists(self.cfg.global_lock) and any(
                w.name == window_name(None, "self-update") and not w.finished for w in self.windows):
            return "busy: boxen self-update is running in its terminal window"
        return None

    # ------------------------------------------------------------ windows

    def _tmux_ok(self, what):
        if in_boxen_tmux(self.env):
            return True
        self.notify("%s needs the dashboard to run in `boxen ui` (tmux)." % what, severity="warning", markup=False,
                    timeout=8)
        return False

    def open_window(self, name, argv, lock=None, cwd=None, new=False):
        if not self._tmux_ok("A terminal window"):
            return
        self.run_worker(self._open_window(name, argv, lock, cwd, new), group="windows", exit_on_error=False)

    async def _open_window(self, name, argv, lock, cwd, new):
        ok, how, err = await self.tmux.open(name, argv, lock=lock, cwd=cwd, new=new)
        if not ok:
            self.notify("tmux: %s" % (err or "couldn't open a window"), severity="error", markup=False)
        await self._refresh_windows()

    # ------------------------------------------------------------ actions

    def _need_project(self):
        p = self.current()
        if p is None:
            self.notify("No project selected.", severity="warning", markup=False)
            return None
        if not valid_name(p.name):
            self.notify("not a valid project name", severity="error", markup=False)
            return None
        return p

    def _need_folder(self, p):
        """The project's folder, where sessions and VM shells start: checked
        to be a folder (not a link to one elsewhere)."""
        if not os.path.isdir(p.path) or os.path.islink(p.path):
            self.notify("%s isn't a folder; not started there" % clean_line(p.path), severity="error", markup=False)
            return None
        return p.path

    def _confirm(self, title, message, then, warn=False, yes="Yes"):
        def answered(ok):
            if ok:
                then()
        self.app.push_screen(ConfirmScreen(title, message, yes=yes, warn=warn), answered)

    def _busy_check(self, project=None, is_global=False):
        why = self.jobs.busy_reason(project, is_global)
        if why:
            self.notify(why, severity="warning", markup=False)
        return why

    def action_claude(self):
        """Switch to the project's Claude session (the last one started),
        or start one."""
        p = self._need_project()
        if not p or not self._tmux_ok("A Claude session") or not self._need_folder(p):
            return

        async def go():
            w = await self.tmux.find_last(p.name)
            if w is not None:
                await self._select_window(w.id)
            else:
                await self._new_claude(p)
        self.run_worker(go(), group="windows", exit_on_error=False)

    def action_claude_new(self):
        p = self._need_project()
        if p and self._tmux_ok("A Claude session") and self._need_folder(p):
            self.run_worker(self._new_claude(p), group="windows", exit_on_error=False)

    async def _new_claude(self, p):
        """Another Claude session window, after closing the project's
        sessions that have exited (each is held for Enter otherwise)."""
        await self.tmux.kill_finished(p.name)
        await self._open_window(p.name, CLAUDE_ARGV, None, p.path, True)

    def action_serve(self):
        p = self._need_project()
        if not p:
            return
        if self._busy_check(p.name):
            return
        if p.serve == "on":
            self._confirm("Turn serve off for %s?" % p.name,
                          "Stops its Remote Control server (window %s): the Claude app can't start sessions "
                          "here until it's on again. %s" % (serve_window(p.name), _serve_sessions_note(p)),
                          lambda: self.start_job(["serve", p.name, "--off"], project=p.name), yes="Turn off")
            return

        def started(_job):
            self.notify("%s: serve on. The first time, it asks to trust the folder and to enable Remote Control: "
                        "answer in its window (Sessions tab, %s)." % (p.name, serve_window(p.name)),
                        markup=False, timeout=12)
        self.start_job(["serve", p.name], project=p.name, then=started)

    def action_up(self):
        p = self._need_project()
        if p:
            self.start_job(["up", p.name], project=p.name)

    def action_down(self):
        p = self._need_project()
        if p and not self._busy_check(p.name):
            self._confirm("Stop %s's build container?" % p.name,
                          "Stops the build container and its sidecars (background processes in it end). The code "
                          "and their data are kept, and Claude sessions keep running; u (or the next boxen run) "
                          "starts it again.",
                          lambda: self.start_job(["down", p.name], project=p.name), yes="Stop")

    def action_shell(self):
        p = self._need_project()
        if p:
            self.open_window(window_name(p.name, "shell"), [self.cfg.boxen, "shell", p.name])

    def action_vm_shell(self):
        """A shell on the VM in the project's folder, as if you'd cd'ed
        there over SSH (outside Claude's sandbox: it's yours)."""
        p = self._need_project()
        if p and self._need_folder(p):
            shell = self.env.get("SHELL") or "/bin/bash"
            self.open_window(window_name(p.name, "vm-shell"), VM_SHELL_ENV + [shell], cwd=p.path)

    def action_logs_tab(self):
        self.query_one("#tabs", TabbedContent).active = "logs"

    def action_sessions_tab(self):
        self.query_one("#tabs", TabbedContent).active = "sessions-tab"

    def action_files_tab(self):
        self.query_one("#tabs", TabbedContent).active = "files-tab"

    def action_new(self):
        def answered(res):
            if res:
                name, url = res
                self.start_job(["new", name] + ([url] if url else []), project=name)
        self.app.push_screen(NewProjectScreen(), answered)

    def action_rm(self):
        p = self._need_project()
        if p and not self._busy_check(p.name):
            self._confirm("Remove %s from boxen?" % p.name,
                          "Removes its build container, image, sidecars and their data (volumes), and boxen's "
                          "settings for it: your compose.override.yaml, its GitHub repos list and tokens, serve. "
                          "The code in %s (with .boxen/) and its Claude history stay." % p.path,
                          lambda: self.start_job(["rm", p.name, "--yes"], project=p.name), warn=True, yes="Remove")

    def action_global_menu(self):
        def chosen(key):
            if key:
                getattr(self, "action_global_" + key)()
        self.app.push_screen(GlobalMenuScreen(), chosen)

    def action_global_update_all(self):
        if not self._busy_check(is_global=True):
            self._confirm("Update everything?", "Rebuilds the base image from a fresh Ubuntu, then every project's "
                          "build container (background processes in them stop). Takes a while; project actions "
                          "wait until it's done. Claude sessions keep running.",
                          lambda: self.start_job(["update"], is_global=True), yes="Update all")

    def action_global_self_update(self):
        if self._busy_check(is_global=True):
            return
        try:
            os.makedirs(self.cfg.state_dir, exist_ok=True)
        except OSError:
            pass
        self.open_window(window_name(None, "self-update"), [self.cfg.boxen, "self-update"],
                         lock=self.cfg.global_lock)

    def action_global_tokens(self):
        self.start_job(["tokens"], is_global=True)

    def action_global_github(self):
        self.open_window(window_name(None, "github"), [self.cfg.boxen, "github"])

    def action_global_doctor(self):
        if not self._busy_check(is_global=True):
            self.query_one("#jobs").remove_class("collapsed")
            self.start_job(["doctor"], is_global=True)

    def action_global_migrate(self):
        if not self._busy_check(is_global=True):
            self.open_window(window_name(None, "migrate"), [self.cfg.boxen, "migrate"])

    def action_toggle_jobs(self):
        self.query_one("#jobs").toggle_class("collapsed")
        self._render_jobs_bar()

    def action_help(self):
        self.app.push_screen(HelpScreen())

    async def action_detach(self):
        if in_boxen_tmux(self.env):
            ok, err = await self.tmux.detach()
            if not ok:
                self.notify("tmux detach failed: %s" % err, severity="error", markup=False)
        else:
            await self.app.shutdown()

    def _jobs_note(self):
        """For the quit and restart questions: the running jobs, by name,
        that will be stopped; "" when there are none."""
        run = self.jobs.running()
        if not run:
            return ""
        names = ", ".join(clean_line(j.display) for j in run)
        return (" These running jobs will be stopped (SIGTERM, then SIGKILL after %d s if they're still going): "
                "%s." % (JOB_GRACE, names))

    def action_quit_all(self):
        note = self._jobs_note()
        msg = ("Stops the dashboard. Claude sessions, serve and the other windows keep running; "
               "`boxen ui` starts it again." + note)

        async def go():
            await self.app.shutdown()
        self._confirm("Quit the dashboard?", msg, lambda: self.run_worker(go(), exit_on_error=False),
                      warn=bool(note), yes="Quit")

    def action_restart_dashboard(self):
        if not in_boxen_tmux(self.env):
            self.notify("Not running under `boxen ui`; just start it again.", severity="warning", markup=False)
            return
        note = self._jobs_note()

        async def go():
            if self.jobs.running():
                self.notify("Stopping the running jobs, then restarting…", markup=False)
                await self.jobs.stop_all()
            # On success tmux starts the dashboard again in this window, and
            # this process goes (SIGHUP) as boxen ui --restart finishes.
            rc, out, _err = await procs.run([self.cfg.boxen, "ui", "--restart"], env=self.jobs.env, timeout=30,
                                            merge_stderr=True)
            if rc != 0:
                lines = [ln for ln in out.strip().splitlines() if ln.strip()]
                self.notify("Couldn't restart the dashboard: %s" % clean_line(lines[-1] if lines else "exit %d" % rc),
                            severity="error", markup=False, timeout=12)
        self._confirm("Restart the dashboard?",
                      "Starts this dashboard again; windows and projects keep running." + note,
                      lambda: self.run_worker(go(), exit_on_error=False), warn=bool(note), yes="Restart")


class BoxenApp(App):
    TITLE = "boxen"
    ENABLE_COMMAND_PALETTE = True
    # ctrl+q is Textual's quit: here it goes through the same question as Q.
    BINDINGS = [Binding("ctrl+q", "quit", "Quit", show=False, priority=True)]

    def __init__(self, cfg=None, env=None):
        super().__init__()
        self.env = dict(os.environ if env is None else env)
        self.cfg = cfg or Config.from_env(self.env)
        self.dashboard = Dashboard(self.cfg, self.env)
        self._shutting_down = False

    def get_default_screen(self):
        return self.dashboard

    def on_mount(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGHUP, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.ensure_future(self.shutdown()))
            except (NotImplementedError, RuntimeError, ValueError):
                pass

    def get_system_commands(self, screen):
        d = self.dashboard
        if screen is d:
            p = d.current()
            if p is not None:
                for action, title, desc in PROJECT_ACTIONS:
                    yield SystemCommand("%s: %s" % (title, clean_line(p.name)), desc, getattr(d, "action_" + action))
            for action, title, desc in GLOBAL_PALETTE:
                yield SystemCommand(title, desc, getattr(d, "action_" + action))

    async def action_quit(self):
        if self.screen is self.dashboard:
            self.dashboard.action_quit_all()

    async def shutdown(self):
        """Stop every job and refresh, then exit. Only the dashboard: the
        other windows in boxen's tmux (Claude sessions, serve) are yours."""
        if self._shutting_down:
            return
        self._shutting_down = True
        # Every worker but this one (Q runs this in a worker): cancelled, it
        # would cut short the jobs' grace period below.
        try:
            me = get_current_worker()
        except NoActiveWorker:
            me = None
        for w in list(self.workers):
            if w is not me:
                w.cancel()
        try:
            await self.dashboard.jobs.stop_all()
        except (Exception, asyncio.CancelledError):
            pass
        procs.kill_all()
        self.exit()


def main():
    app = BoxenApp()
    try:
        app.run()
    finally:
        procs.kill_all()


__all__ = ["BoxenApp", "Dashboard", "editor_argv", "GLOBAL", "main"]
