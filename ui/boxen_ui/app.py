"""boxen ui: the dashboard.

Data comes in off the UI thread, on a one-second tick that decides what's
due: `boxen ls --json` (a subprocess) every 5 s while someone is looking
(a client attached to the tmux session and the terminal focused), else
every 60 s, and at once after every job and on ctrl+r; container states
and stats from the Docker Engine API every 3 s while a client is attached
(a thread); the VM summary every 5 s (a thread); tmux windows every second
on the Terminals tab, else every 3 s. Actions run `boxen` as jobs (see
jobs.py) or open tmux windows (tmux.py). Nothing from outside is ever
rendered as markup: see textutil.py.
"""
import asyncio
import collections
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
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

from . import edit, files, procs, sysinfo
from .config import PKG_DIR, Config, valid_name
from .docker_api import DockerAPI, DockerError, cpu_percent, memory
from .jobs import APPROVAL, FAILED, OK, RUNNING, STOPPED, Busy, JobManager, fmt_elapsed, question_project
from .model import (AMBER, GREEN, GREY, HEALTH_STYLE, RED, RUNNING_JOB, SERVER_STYLE, container_short,
                    container_style, github_short, parse_ls, server_short, uptime)
from .screens import (ApprovalScreen, ConfirmScreen, GlobalMenuScreen, HelpScreen, NewProjectScreen,
                      QuestionScreen)
from .textutil import ansi_text, clean_line, text
from .tmux import GLOBAL, Tmux, in_boxen_tmux, window_name

UI_DIR = os.path.dirname(PKG_DIR)
SEP = "--sep--"
HIST = 60
HIST_FORGET = 300           # drop a container's series after it's been gone this long
SESSION_ID = re.compile(r"^[0-9a-f]{8}$")
JOB_STYLE = {RUNNING: RUNNING_JOB, OK: GREEN, FAILED: RED, APPROVAL: AMBER, STOPPED: GREY}
SESSION_STYLE = {"live": GREEN, "partial": AMBER, "carried": GREY}
LS_ACTIVE, LS_IDLE, STATS_EVERY, SYS_EVERY = 5, 60, 3, 5

PROJECT_ACTIONS = [
    # (action, palette title, description)
    ("up", "Up", "start, or apply env/compose changes"),
    ("down", "Down", "stop the container (asks first)"),
    ("restart", "Restart", "recreate the container (asks first)"),
    ("update_project", "Update", "rebuild the project's image and recreate it (asks first)"),
    ("shell", "Shell", "bash inside the container, in a terminal window"),
    ("claude", "Claude", "a terminal Claude session inside the container"),
    ("logs_tab", "Logs", "the server's output"),
    ("follow_logs", "Follow logs", "the server's output, live, in a terminal window"),
    ("sessions_tab", "Sessions", "earlier conversations: continue one in the Claude app"),
    ("terminals_tab", "Terminals", "the project's terminal windows, with a live preview"),
    ("files_tab", "Files", "the project's env files: edit one in vim"),
    ("rm", "Remove", "remove the project's container, image and config (asks; keeps the code)"),
]
GLOBAL_PALETTE = [
    ("new", "New project", "create a project, optionally cloning a GitHub repo"),
    ("global_update_all", "Update everything", "rebuild the base image and every project"),
    ("global_self_update", "Self-update boxen", "pull the latest boxen and apply it (terminal)"),
    ("global_tokens", "Refresh GitHub tokens", "mint fresh tokens for every project"),
    ("global_github", "Connect GitHub", "GitHub App and commit identity (terminal)"),
    ("global_login", "Sign in to Claude", "shared by every project (terminal)"),
    ("global_import_mac", "Moving from a Mac", "how to bring your work over (terminal)"),
    ("global_supervisor", "Set up the supervisor", "run boxen from the Claude app (terminal)"),
    ("refresh", "Refresh", "look at everything again now"),
    ("toggle_jobs", "Jobs panel", "show or hide the jobs panel"),
    ("restart_dashboard", "Restart dashboard", "start the dashboard again (after a boxen update)"),
    ("help", "Keys", "every key boxen ui knows"),
    ("detach", "Detach", "leave boxen ui running; `boxen ui` comes back"),
    ("quit_all", "Quit boxen ui", "stop jobs and close every boxen ui window"),
]
GETTING_STARTED = """No projects yet.

  n   new project: a name, a GitHub repo (optional)
  g   setup: GitHub, Claude sign-in, the supervisor,
      and "Moving from a Mac" (bring your work over)

More in the README: "Create a project" and
"Moving from a Mac", in
%s"""


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


def _fmt_cpu(v):
    if v is None:
        return Text("-", style=GREY)
    return Text(("%.1f%%" % v) if v < 10 else ("%d%%" % round(v)))


def _fmt_mem(v):
    return Text("-", style=GREY) if v is None else Text(sysinfo.human_bytes(v))


def _ellipsis(s, width):
    return s if len(s) <= width else s[:max(0, width - 1)] + "…"


class ProjectTable(DataTable):
    BINDINGS = [Binding("j", "cursor_down", "Down", show=False),
                Binding("k", "cursor_up", "Up", show=False)]


class SessionTable(DataTable):
    """A click only moves the cursor; Enter or a double click acts."""

    async def _on_click(self, event):
        meta = event.style.meta
        row = meta.get("row") if meta else None
        if isinstance(row, int) and 0 <= row < self.row_count:
            self.move_cursor(row=row)
            if event.chain >= 2:
                self.action_select_cursor()
        event.stop()


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
    #sidecars { height: auto; max-height: 8; }
    #ov-apply, #ov-status { height: auto; color: $text-muted; }
    #sess-status, #logs-status, #term-status, #files-note, #sess-help { height: auto; color: $text-muted; }
    #sessions { height: 1fr; }
    #logs-bar { height: 3; }
    #log-source { width: 30; }
    #logs-bar Static { padding: 1 0 0 2; color: $text-muted; }
    #log-view { height: 1fr; }
    #term-list { height: auto; max-height: 7; }
    #term-preview { height: 1fr; border: round $panel; padding: 0 1; overflow: hidden; }
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
        Binding("u", "up", "Up"),
        Binding("d", "down", "Down"),
        Binding("r", "restart", "Restart"),
        Binding("U", "update_project", "Update"),
        Binding("s", "shell", "Shell"),
        Binding("c", "claude", "Claude"),
        Binding("n", "new", "New"),
        Binding("g", "global_menu", "Global"),
        Binding("J", "toggle_jobs", "Jobs"),
        Binding("f", "follow_logs", "Follow"),
        Binding("a", "session_attach", "Attach"),
        Binding("x", "session_stop", "Close"),
        Binding("e", "edit_file", "Edit"),
        Binding("ctrl+r,f5", "refresh", "Refresh", show=False),
        Binding("l", "logs_tab", "Logs", show=False),
        Binding("X", "rm", "Remove", show=False),
        Binding("Q", "quit_all", "Quit", show=False),
    ]
    # Keys that only mean something on one tab: shown (and live) only there.
    TAB_ACTIONS = {"follow_logs": ("logs",), "session_attach": ("sessions-tab",),
                   "session_stop": ("sessions-tab",), "edit_file": ("files-tab",)}

    def __init__(self, cfg, env):
        super().__init__()
        self.cfg = cfg
        self.env = env
        self.tmux = Tmux(cfg.tmux, cfg.tmux_socket, cfg.tmux_session, env=env, home=cfg.home)
        self.jobs = JobManager(cfg.boxen, self, env=env)
        self.jobs.external_global = self._external_global
        self.projects = []
        self.by_name = {}
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
        self.compact = None
        self.selected_job = None
        self.pending_edits = {}     # result.json path -> edit spec
        self._busy = set()          # refreshes in flight
        self._due = {}              # what -> monotonic time it last ran
        self._ticks = 0
        self._last_row = 0
        self._log_source = "server"
        self._sessions = []
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
                                    for key in ("container", "server", "github", "code"):
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
                                yield Static("Sidecars", classes="section")
                                yield DataTable(id="sidecars", cursor_type="none", show_cursor=False)
                                yield Static("Last apply", classes="section")
                                yield Static(Text(""), id="ov-apply")
                                yield Static("STATUS.md", classes="section")
                                yield Static(Text(""), id="ov-status")
                    with TabPane("Sessions", id="sessions-tab"):
                        yield Static(Text(""), id="sess-status")
                        yield SessionTable(id="sessions", cursor_type="row")
                        yield Static(Text("⏎ continue in app · a terminal · x close · ctrl+r refresh"),
                                     id="sess-help")
                    with TabPane("Logs", id="logs"):
                        with Horizontal(id="logs-bar"):
                            yield Select([(Text("server"), "server")], id="log-source", allow_blank=False,
                                         value="server")
                            yield Static(Text("ctrl+r refresh · f follow in a terminal"))
                        yield Static(Text(""), id="logs-status")
                        yield Log(id="log-view", max_lines=2000)
                    with TabPane("Terminals", id="terminals"):
                        yield Static(Text(""), id="term-status")
                        yield DataTable(id="term-list", cursor_type="row")
                        yield Static(Text(""), id="term-preview")
                    with TabPane("Files", id="files-tab"):
                        yield Static(Text("e edits a private copy of the selected file in vim ($EDITOR if set; "
                                          "modelines off), saved back when you quit it. A full editor comes in "
                                          "phase 2."), id="files-note")
                        yield DataTable(id="files", cursor_type="row")
        with Vertical(id="jobs", classes="collapsed"):
            yield Static(Text(""), id="jobs-bar")
            with Horizontal(id="jobs-body"):
                yield DataTable(id="job-table", cursor_type="row")
                yield Log(id="job-log", max_lines=3000)
        yield Footer()

    def on_mount(self):
        ss = self.query_one("#sessions", DataTable)
        ss.add_column(Text("", style="bold"), key="n")
        tl = self.query_one("#term-list", DataTable)
        tl.add_column(Text("WINDOW", style="bold"), key="name")
        tl.add_column(Text("STATE", style="bold"), key="state")
        fl = self.query_one("#files", DataTable)
        for label, key in (("FILE", "label"), ("PATH", "path"), ("", "state")):
            fl.add_column(Text(label, style="bold"), key=key)
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
        now = time.monotonic()
        if now - self._due.get(what, -1e9) >= every:
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
        # tmux windows: every second on the Terminals tab, else every 3 s
        # (the attach state, the self-update lock).
        if "tick" in self._busy or (self.active_tab() != "terminals" and self._ticks % 3):
            return
        self._busy.add("tick")
        try:
            await self._refresh_terminals()
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
        t.append("   agent ")
        agent = s.get("agent")
        has_sup = any(p.supervisor for p in self.projects)
        if agent:
            t.append("up", style=GREEN)
        elif agent is None:
            t.append("?", style=GREY)
        elif has_sup:
            t.append("down", style=AMBER)
        else:
            t.append("down (supervisor not set up)", style=GREY)
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
        if self.git and self.git_start and self.git != self.git_start:
            b.update(Text.assemble(("boxen was updated ", "bold"), "(%s → %s): restart the dashboard to use it: "
                                   "ctrl+p → Restart dashboard, or boxen ui --restart" % (self.git_start, self.git)))
            b.add_class("shown")
        else:
            b.remove_class("shown")

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
        out = {"load": sysinfo.load(), "mem": sysinfo.memory(), "disk_root": sysinfo.disk_free("/"),
               "agent": sysinfo.agent_up(self.cfg.agent_sock)}
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
                    projects, docker = parse_ls(out)
                except ValueError as e:
                    self.ls_error = str(e)
                else:
                    self.ls_error = None
                    first = not self.projects
                    self.projects = projects
                    self.ls_docker = docker
                    self.by_name = {p.name: p for p in projects}
                    if first:
                        self.refresh_stats()
            self.ls_loaded = True
            self._render_projects(force=True if self._stale_changed() else False)
            self._render_header()
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
                ("CTR" if compact else "CONTAINER", "ctr"), ("SERVER", "srv"), ("CPU", "cpu"), ("MEM", "mem")]
        if not compact:
            cols.append(("GITHUB", "gh"))
        for label, key in cols:
            t.add_column(Text(label, style="bold"), key=key)
        sc = self.query_one("#sidecars", DataTable)
        sc.clear(columns=True)
        cols = [("SERVICE", "svc")] + ([] if compact else [("IMAGE", "img")]) + \
               [("STATUS", "st"), ("HEALTH", "h"), ("CPU", "cpu"), ("MEM", "mem")]
        for label, key in cols:
            sc.add_column(Text(label, style="bold"), key=key)
        self._render_projects(force=True)
        self._render_sidecars()
        if self._sessions:
            self._render_sessions()

    def _container_text(self, name):
        """(state, health) of a container as Docker says now, else None."""
        return self.live.get(name)

    def _project_cells(self, p):
        stale = bool(self.ls_error)
        name = Text(clean_line(p.name)[:24], style="bold cyan" if p.supervisor else "bold")
        if not self.ls_docker:
            ctr_t, srv_t = Text("?", style=GREY), Text("?", style=GREY)
        else:
            ctr = container_short(p)
            style = container_style(p)
            if p.container is not None and p.container.restarts:
                ctr_t = Text("%s ↻%d" % (clean_line(ctr), p.container.restarts), style=style)
            else:
                ctr_t = Text(clean_line(ctr), style=style)
            srv = server_short(p)
            srv_t = Text(clean_line(srv), style=SERVER_STYLE.get(srv, ""))
        st = self.stats.get("boxen-%s" % p.name, {}) if p.running else {}
        cells = [name, ctr_t, srv_t, _fmt_cpu(st.get("cpu")), _fmt_mem(st.get("mem"))]
        if not self.compact:
            g, style = github_short(p)
            cells.append(Text(clean_line(g), style=style))
        if stale:
            for c in cells:
                c.stylize("dim")
        return cells

    def _render_projects(self, force=False):
        t = self.query_one("#projects", ProjectTable)
        keys = []
        for p in self.projects:
            if p.supervisor and keys:
                keys.append(SEP)
            keys.append(p.name)
        have = [k.value for k in t.rows]
        ncols = len(t.columns)
        if force or have != keys:
            sel = self.selected
            t.clear()
            for k in keys:
                if k == SEP:
                    t.add_row(*[Text("─" * 4, style=GREY)] * ncols, key=SEP)
                else:
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
                if k == SEP:
                    continue
                for ck, cell in zip(colkeys, self._project_cells(self.by_name[k]), strict=True):
                    t.update_cell(k, ck, cell)

    @on(DataTable.RowHighlighted, "#projects")
    def _project_highlighted(self, ev):
        key = ev.row_key.value if ev.row_key is not None else None
        t = self.query_one("#projects", ProjectTable)
        if key == SEP:
            step = 1 if ev.cursor_row >= self._last_row else -1
            target = ev.cursor_row + step
            if not 0 <= target < t.row_count:
                target = ev.cursor_row - step
            t.move_cursor(row=target)
            return
        self._last_row = ev.cursor_row
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
        t = Text()
        t.append(clean_line(p.name), style="bold")
        if p.supervisor:
            t.append("  (supervisor: runs boxen for you from the Claude app)", style=GREY)
        title.update(t)

        def val(key, *parts):
            v = Text(no_wrap=True, overflow="ellipsis")
            for part in parts:
                if isinstance(part, Text):
                    v.append_text(part)
                else:
                    v.append(part)
            self.query_one("#ov-" + key, Static).update(v)

        c = p.container
        if not self.ls_docker:
            val("container", Text("unknown: Docker isn't answering", style=GREY))
        elif c is None:
            val("container", Text("not created", style=GREY))
        else:
            up = uptime(c.started_at) if c.status == "running" else None
            val("container", Text(clean_line(c.status), style=container_style(p)),
                "  up %s" % up if up else "", "  %d restart%s" % (c.restarts, "" if c.restarts == 1 else "s"),
                Text("  %s" % clean_line(c.id[:12]), style=GREY))
        srv = server_short(p)
        val("server", Text(clean_line(p.server if p.running else "stopped"), style=SERVER_STYLE.get(srv, "")))
        g, gstyle = github_short(p)
        val("github", clean_line(p.repo) if p.repo else Text("no GitHub remote", style=GREY),
            "  ", Text(clean_line(p.github), style=gstyle))
        val("code", clean_line(os.path.join(self.cfg.src_root, p.name)))
        self._render_stats()
        self._render_sidecars()
        self._thread(self._overview_files_thread, p.name)

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
            if live is None:
                status, health = ("not created", None) if self.docker_ok else ("?", None)
            else:
                status, health = live
            st = self.stats.get(sc.container, {}) if status == "running" else {}
            cells = [Text(clean_line(sc.service), style="bold")]
            if not self.compact:
                cells.append(text(sc.image))
            cells += [Text(clean_line(status), style={"running": GREEN, "restarting": RED,
                                                      "dead": RED}.get(status, GREY)),
                      Text(clean_line(health or "-"), style=HEALTH_STYLE.get(health or "", GREY)),
                      _fmt_cpu(st.get("cpu")), _fmt_mem(st.get("mem"))]
            t.add_row(*cells)

    def _render_stats(self):
        p = self.current()
        if p is None:
            return
        cpu_h, mem_h = self.hist.get(p.name, ((), ()))
        self.query_one("#spark-cpu", Sparkline).data = list(cpu_h)
        self.query_one("#spark-mem", Sparkline).data = list(mem_h)
        st = self.stats.get("boxen-%s" % p.name, {}) if p.running else {}
        self.query_one("#cpu-now", Static).update(_fmt_cpu(st.get("cpu")) if p.running else Text("stopped", style=GREY))
        m, lim = st.get("mem"), st.get("limit")
        mt = _fmt_mem(m)
        if m is not None and lim:
            mt.append(" / %s" % sysinfo.human_bytes(lim), style=GREY)
        self.query_one("#mem-now", Static).update(mt if p.running else Text("stopped", style=GREY))

    def _overview_files_thread(self, name):
        pdir = self.cfg.proj_dir(name)
        try:
            apply = files.tail_lines(os.path.join(pdir, "agent.log"), 20)
            apply_t = (Text("\n".join(clean_line(x) for x in apply)) if apply
                       else Text("nothing applied from inside the project yet", style=GREY))
        except (files.Refused, OSError) as e:
            apply_t = Text("agent.log: %s; not shown" % e, style=RED)
        try:
            st = files.read_env_file(pdir, "STATUS.md")
            status_t = text(st.rstrip("\n")) if st else Text("not written yet (boxen writes it at each start)",
                                                             style=GREY)
        except files.Refused as e:
            status_t = Text("%s; not shown" % e, style=RED)
        except OSError as e:
            status_t = Text("env/STATUS.md: %s" % e, style=RED)
        self._call(self._overview_files_done, name, apply_t, status_t)

    def _overview_files_done(self, name, apply_t, status_t):
        if name != self.selected:
            return
        self.query_one("#ov-apply", Static).update(apply_t)
        self.query_one("#ov-status", Static).update(status_t)

    # ------------------------------------------------------------ stats

    def refresh_stats(self):
        if "stats" in self._busy or self.cfg.docker_sock is None or self.docker_ok is False:
            return
        known = set()
        for p in self.projects:
            known.add("boxen-%s" % p.name)
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
            st = stats.get("boxen-%s" % p.name) if p.running else None
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
        if tab == "sessions-tab":
            self.load_sessions()
        elif tab == "logs":
            self._sync_log_sources()
            self.load_logs()
        elif tab == "terminals":
            self.run_worker(self._refresh_terminals(), group="terminals", exclusive=True, exit_on_error=False)
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

    # sessions

    def load_sessions(self):
        self.run_worker(self._sessions_worker(self.selected), group="sessions", exclusive=True,
                        exit_on_error=False)

    async def _sessions_worker(self, name):
        status = self.query_one("#sess-status", Static)
        table = self.query_one("#sessions", DataTable)
        keep = self._selected_session() if self._loaded.get("sessions-tab") == name else None
        table.clear()
        self._sessions = []
        p = self.by_name.get(name) if name else None
        if p is None:
            status.update(Text("no project selected", style=GREY))
            return
        if not p.running:
            status.update(Text("%s isn't running; start it with u to see its sessions." % clean_line(name)))
            return
        status.update(Text("loading sessions…", style=GREY))
        rc, out, err = await procs.run(["docker", "exec", "boxen-%s" % name, "boxen-carry", "list"],
                                       env=self.jobs.env, timeout=90)
        if name != self.selected:
            return
        if rc != 0:
            msg = (err or out).strip().splitlines()
            status.update(Text("couldn't list sessions: %s" % clean_line(msg[-1] if msg else "exit %d" % rc),
                               style=RED))
            return
        rows, seen = [], set()
        for line in out.split("\n"):
            if not line.strip():
                continue
            f = line.split("\t", 6)
            if len(f) != 7 or not f[0].isdigit() or not SESSION_ID.match(f[1]):
                continue
            if f[1] in seen:
                self.log.warning("boxen-carry list: session %s listed twice; keeping the first" % f[1])
                continue
            seen.add(f[1])
            rows.append(f)
        self._sessions = rows
        self._render_sessions(keep)
        status.update(Text("%d session%s" % (len(rows), "" if len(rows) == 1 else "s") if rows
                           else "No earlier sessions in this project yet.", style=GREY))

    def _render_sessions(self, keep=None):
        """The sessions table, its columns by priority: STATE, TITLE,
        TURNS/PARTS, DATE (dropped first when narrow), ID (wide only)."""
        table = self.query_one("#sessions", DataTable)
        if keep is None:
            keep = self._selected_session()
        cols = [("#", "n", 3), ("STATE", "state", 7), ("TITLE", "title", 0), ("TURNS", "turns", 5),
                ("PARTS", "parts", 5), ("DATE", "date", 10), ("ID", "id", 8)]
        avail = (self.query_one("#right").size.width or 80) - 4

        def title_width():
            return avail - sum(w + 2 for _l, _k, w in cols if w) - 2
        # Narrow: ID goes first, then DATE, then PARTS and TURNS; never
        # STATE, and TITLE keeps the rest.
        for drop in ("id", "date", "parts", "turns"):
            if title_width() < 32:
                cols = [c for c in cols if c[1] != drop]
        title_w = max(12, title_width())
        table.clear(columns=True)
        for label, key, w in cols:
            table.add_column(Text(label, style="bold"), key=key, width=(title_w if key == "title" else None))
        for f in self._sessions:
            st = clean_line(f[5])
            cells = {"n": Text(f[0]), "state": Text(st, style=SESSION_STYLE.get(st, "")),
                     "title": Text(_ellipsis(clean_line(f[6]), title_w)), "turns": text(f[3]),
                     "parts": text(f[4]), "date": text(f[2]), "id": Text(f[1], style="cyan")}
            table.add_row(*[cells[k] for _l, k, _w in cols], key=f[1])
        ids = [f[1] for f in self._sessions]
        if keep in ids:
            table.move_cursor(row=ids.index(keep))

    def _selected_session(self):
        t = self.query_one("#sessions", DataTable)
        if not self._sessions or t.row_count == 0:
            return None
        try:
            key = t.coordinate_to_cell_key((t.cursor_row, 0)).row_key.value
        except Exception:
            return None
        return key if key and SESSION_ID.match(key) else None

    @on(DataTable.RowSelected, "#sessions")
    def _session_selected(self, ev):
        sid = ev.row_key.value if ev.row_key is not None else None
        p = self.current()
        if not p or not sid or not SESSION_ID.match(sid):
            return
        self.start_job(["resume", p.name, sid], project=p.name)

    def action_session_attach(self):
        p, sid = self.current(), self._selected_session()
        if p and sid:
            self.open_window(window_name(p.name, "session-" + sid),
                             [self.cfg.boxen, "resume", p.name, sid, "--attach"])

    def action_session_stop(self):
        p, sid = self.current(), self._selected_session()
        if p and sid:
            self.start_job(["resume", p.name, sid, "--stop"], project=p.name)

    # logs

    def _sync_log_sources(self):
        p = self.current()
        opts = [(Text("server"), "server")]
        if p:
            opts += [(Text("sidecar: %s" % clean_line(sc.service)), sc.service) for sc in p.sidecars]
        sel = self.query_one("#log-source", Select)
        values = [v for _l, v in opts]
        if self._log_source not in values:
            self._log_source = "server"
        with sel.prevent(Select.Changed):
            sel.set_options(opts)
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
        if source == "server":
            argv = [self.cfg.boxen, "logs", name, "--tail", "300"]
            what = "boxen logs %s --tail 300" % name
        else:
            sc = self._sidecar(p, source)
            if sc is None:
                return
            argv = ["docker", "logs", "--tail", "300", sc.container]
            what = "docker logs --tail 300 %s" % sc.container
        status.update(Text("loading: %s" % clean_line(what), style=GREY))
        rc, out, _err = await procs.run(argv, env=self.jobs.env, timeout=60, merge_stderr=True)
        if name != self.selected or source != self._log_source:
            return
        lines = [clean_line(x) for x in out.splitlines()]
        view.write_lines(lines)
        status.update(Text("%s · %d lines%s" % (clean_line(what), len(lines), "" if rc == 0 else " · exit %d" % rc),
                           style=GREY if rc == 0 else RED))

    def action_follow_logs(self):
        p = self.current()
        if not p:
            return
        if self._log_source == "server":
            self.open_window(window_name(p.name, "logs"), [self.cfg.boxen, "logs", p.name, "-f"])
        else:
            sc = self._sidecar(p, self._log_source)
            if sc:
                self.open_window(window_name(p.name, "logs-" + sc.service),
                                 ["docker", "logs", "-f", "--tail", "100", sc.container])

    # terminals

    async def _refresh_terminals(self):
        if not in_boxen_tmux(self.env):
            self.windows = []
        else:
            self.windows = await self.tmux.list_windows()
        if self.active_tab() != "terminals":
            return
        p = self.selected
        status = self.query_one("#term-status", Static)
        table = self.query_one("#term-list", DataTable)
        preview = self.query_one("#term-preview", Static)
        if not in_boxen_tmux(self.env):
            status.update(Text("Not running inside `boxen ui`'s tmux session: terminal windows are "
                               "unavailable. Start the dashboard with `boxen ui`.", style=AMBER))
            return
        mine = [w for w in self.windows if p and w.name.startswith(p + "/")]
        rows = [(w.id, w.name, w.finished) for w in mine]
        if getattr(self, "_term_rows", None) != rows:
            self._term_rows = rows
            cur = table.cursor_row
            table.clear()
            for w in mine:
                table.add_row(text(w.name), Text("exited" if w.finished else "running",
                                                 style=GREY if w.finished else GREEN), key=w.id)
            if mine:
                table.move_cursor(row=min(cur, len(mine) - 1))
        if not mine:
            status.update(Text("No terminal windows for this project. s opens a shell, c Claude, "
                               "f (Logs) follows the log.", style=GREY))
            preview.update(Text(""))
            return
        status.update(Text("Enter switches to the window · F1 (or C-b 0) comes back here", style=GREY))
        try:
            wid = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value
        except Exception:
            return
        cap = await self.tmux.capture(wid)
        if cap is None or self.active_tab() != "terminals":
            return
        lines = cap.rstrip("\n").split("\n")
        while lines and not lines[-1].strip():
            lines.pop()
        h = max(1, preview.size.height or 10)
        preview.update(ansi_text("\n".join(lines[-h:])))

    @on(DataTable.RowSelected, "#term-list")
    def _terminal_selected(self, ev):
        wid = ev.row_key.value if ev.row_key is not None else None
        if wid:
            self.run_worker(self._select_window(wid), exit_on_error=False)

    async def _select_window(self, wid):
        ok, err = await self.tmux.select(wid)
        if not ok:
            self.notify("tmux: %s" % (err or "couldn't switch"), severity="error", markup=False)

    # files

    def _file_entries(self, p):
        """(label, path, in env/)."""
        pdir = self.cfg.proj_dir(p.name)
        out = [("env/Dockerfile", os.path.join(pdir, "env", "Dockerfile"), True)]
        if not p.supervisor:
            out.append(("env/services.yaml", os.path.join(pdir, "env", "services.yaml"), True))
        out.append(("compose.override.yaml", os.path.join(pdir, "compose.override.yaml"), False))
        out.append(("project.env", os.path.join(pdir, "project.env"), False))
        if not p.supervisor:
            out.append(("CLAUDE.md (code)", os.path.join(self.cfg.src_root, p.name, "CLAUDE.md"), False))
        return out

    def _render_files(self):
        t = self.query_one("#files", DataTable)
        t.clear()
        p = self.current()
        if p is None:
            return
        for label, path, _env in self._file_entries(p):
            exists = os.path.lexists(path)
            t.add_row(Text(label, style="bold"), text(path),
                      Text("" if exists else "(new)", style=GREY), key=path)

    def action_edit_file(self):
        p = self.current()
        t = self.query_one("#files", DataTable)
        if p is None or t.row_count == 0:
            return
        entries = self._file_entries(p)
        label, path, env_file = entries[min(max(t.cursor_row, 0), len(entries) - 1)]
        if not in_boxen_tmux(self.env):
            self.notify("Editing needs the dashboard to run in `boxen ui` (tmux).", severity="warning",
                        markup=False)
            return
        self.run_worker(self._edit(p.name, label, path, env_file), group="windows", exit_on_error=False)

    async def _edit(self, name, label, path, env_file):
        wname = window_name(name, "edit-" + os.path.basename(path))
        w = await self.tmux.find(wname)
        if w is not None and not w.finished:
            await self.tmux.select(w.id)
            return
        try:
            spec = edit.prepare(name, label, path, self.cfg.proj_dir(name), env_file, editor_argv(self.env),
                                env=self.env)
        except (files.Refused, OSError) as e:
            self.notify("%s: %s; not opened" % (label, e), severity="error", markup=False)
            return
        argv = ["env", "PYTHONPATH=" + UI_DIR, sys.executable, "-P", "-m", "boxen_ui.edit",
                os.path.join(spec["dir"], "spec.json")]
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
        if status == "written" and label == "env/services.yaml":
            self.notify("%s: saved %s; checking it (boxen restart %s --prepare)" % (name, label, name),
                        markup=False)
            job = self.start_job(["restart", name, "--prepare"], project=name)
            if job:
                job.followup = "apply"
        elif status == "written" and label == "env/Dockerfile":
            self.notify("%s: saved %s; U rebuilds the image with it" % (name, label), markup=False)
        elif status == "written":
            self.notify("%s: saved %s" % (name, label), markup=False)
        elif status in ("conflict", "refused"):
            self.notify("%s: %s" % (name, msg), severity="error", markup=False, timeout=12)
        if self.active_tab() == "files-tab":
            self._render_files()

    # ------------------------------------------------------------ jobs

    def start_job(self, args, project=None, is_global=False):
        if project is not None and not valid_name(project):
            self.notify("not a valid project name", severity="error", markup=False)
            return None
        try:
            job = self.jobs.start(args, project=project, is_global=is_global)
        except Busy as e:
            self.notify(str(e), severity="warning", markup=False)
            return None
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

    @on(DataTable.RowSelected, "#job-table")
    def _job_selected(self, ev):
        key = ev.row_key.value if ev.row_key is not None else None
        job = self._job(int(key)) if key else None
        if job and job.status == APPROVAL:
            self._ask(job)

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
            parts = ["%d %s" % (counts[s], s) for s in (RUNNING, APPROVAL, FAILED) if counts[s]]
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
        elif job.status == FAILED:
            last = job.lines[-1] if job.lines else "exit %s" % job.rc
            self.notify("%s failed: %s" % (job.title, clean_line(last)[:200]), severity="error", markup=False,
                        timeout=8)
        if job.status == APPROVAL or (job.status == FAILED and job.needs_terminal()):
            self._ask(job)
        if job.status == OK and getattr(job, "followup", None) == "apply" and job.project:
            name = job.project
            self._confirm("Apply %s's services.yaml now?" % name,
                          "It checks out. Applying it recreates the container (boxen restart %s): the "
                          "server restarts and app sessions reconnect." % name,
                          lambda: self.start_job(["restart", name], project=name), yes="Apply now")
        # The state changed: look again now rather than at the next poll.
        self.call_later(self.refresh_ls)
        if job.project == self.selected and job.verb == "resume" and self.active_tab() == "sessions-tab":
            self.load_sessions()

    def _ask(self, job):
        if job.status == APPROVAL and job.approval is not None:
            a = job.approval

            def answered(yes):
                if yes:
                    self.start_job(a.argv[1:], project=a.name)
            self.app.push_screen(ApprovalScreen(a, job.title), answered)
            return
        lines = job.lines[-20:]
        if job.status == APPROVAL:
            project = question_project(job.lines, job.project)
            title = "%s needs an answer only a terminal can give" % job.title
            argv = [self.cfg.boxen, "up", project] if project else None
            wname = window_name(project, "up") if project else None
        else:
            title = "%s needs a terminal" % job.title
            argv, wname = list(job.argv), window_name(job.project, job.verb)

        def answered(choice):
            if choice == "terminal" and argv and wname:
                self.open_window(wname, argv)
        self.app.push_screen(QuestionScreen(title, lines, can_open=bool(argv and wname)), answered)

    def _external_global(self):
        """A global command in a terminal window (self-update) is running."""
        if os.path.exists(self.cfg.global_lock) and any(
                w.name == window_name(None, "self-update") and not w.finished for w in self.windows):
            return "busy: boxen self-update is running in its terminal window"
        return None

    # ------------------------------------------------------------ windows

    def open_window(self, name, argv, lock=None):
        if not in_boxen_tmux(self.env):
            self.notify("Terminal windows need the dashboard to run in `boxen ui` (tmux). Command: %s"
                        % " ".join(argv), severity="warning", markup=False, timeout=8)
            return
        self.run_worker(self._open_window(name, argv, lock), group="windows", exit_on_error=False)

    async def _open_window(self, name, argv, lock):
        ok, how, err = await self.tmux.open(name, argv, lock=lock)
        if not ok:
            self.notify("tmux: %s" % (err or "couldn't open a window"), severity="error", markup=False)

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

    def action_up(self):
        p = self._need_project()
        if p:
            self.start_job(["up", p.name], project=p.name)

    def action_down(self):
        p = self._need_project()
        if p and not self._busy_check(p.name):
            self._confirm("Stop %s?" % p.name, "Stops the container (and its sidecars). The code, config and "
                          "home folder are kept; u starts it again.",
                          lambda: self.start_job(["down", p.name], project=p.name), yes="Stop")

    def action_restart(self):
        p = self._need_project()
        if p and not self._busy_check(p.name):
            self._confirm("Restart %s?" % p.name, "Recreates the container, picking up env changes: its "
                          "server restarts and app sessions reconnect.",
                          lambda: self.start_job(["restart", p.name], project=p.name), yes="Restart")

    def action_update_project(self):
        p = self._need_project()
        if p and not self._busy_check(p.name):
            self._confirm("Update %s?" % p.name, "Rebuilds the project's image from env/Dockerfile and recreates "
                          "the container: its server restarts and app sessions reconnect.",
                          lambda: self.start_job(["update", p.name], project=p.name), yes="Rebuild")

    def action_shell(self):
        p = self._need_project()
        if p:
            self.open_window(window_name(p.name, "shell"), [self.cfg.boxen, "shell", p.name])

    def action_claude(self):
        p = self._need_project()
        if p:
            self.open_window(window_name(p.name, "claude"), [self.cfg.boxen, "claude", p.name])

    def action_logs_tab(self):
        self.query_one("#tabs", TabbedContent).active = "logs"

    def action_sessions_tab(self):
        self.query_one("#tabs", TabbedContent).active = "sessions-tab"

    def action_terminals_tab(self):
        self.query_one("#tabs", TabbedContent).active = "terminals"

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
        if p:
            # boxen asks (exit 3, approval needed): the answer is the approval dialog.
            self.start_job(["rm", p.name], project=p.name)

    def action_global_menu(self):
        def chosen(key):
            if key:
                getattr(self, "action_global_" + key)()
        self.app.push_screen(GlobalMenuScreen(), chosen)

    def action_global_update_all(self):
        if not self._busy_check(is_global=True):
            self._confirm("Update everything?", "Rebuilds the base image from a fresh Ubuntu with the newest "
                          "Claude Code, then every project. Takes a while; every server restarts and app "
                          "sessions reconnect. Project actions wait until it's done.",
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

    def action_global_login(self):
        self.open_window(window_name(None, "login"), [self.cfg.boxen, "login"])

    def action_global_import_mac(self):
        self.open_window(window_name(None, "import-mac"), [self.cfg.boxen, "import-mac"])

    def action_global_supervisor(self):
        self.open_window(window_name(None, "supervisor"), [self.cfg.boxen, "supervisor"])

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

    def action_quit_all(self):
        n = len(self.jobs.running())
        msg = ("Stops boxen ui and closes every window in its tmux session (shells, Claude, logs). "
               "Projects keep running.")
        if n:
            msg += " %d running job%s will be stopped." % (n, "" if n == 1 else "s")

        async def go():
            await self.app.shutdown(kill_session=True)
        self._confirm("Quit boxen ui?", msg, lambda: self.run_worker(go(), exit_on_error=False), warn=True,
                      yes="Quit")

    def action_restart_dashboard(self):
        if not in_boxen_tmux(self.env):
            self.notify("Not running under `boxen ui`; just start it again.", severity="warning", markup=False)
            return
        n = len(self.jobs.running())

        def go():
            try:
                subprocess.Popen([self.cfg.boxen, "ui", "--restart"], env=self.app.env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as e:
                self.notify("couldn't restart: %s" % e, severity="error", markup=False)
        self._confirm("Restart the dashboard?",
                      "Starts this dashboard again%s; windows and projects keep running."
                      % (" (stopping %d running job%s)" % (n, "" if n == 1 else "s") if n else ""),
                      go, yes="Restart")


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

    async def shutdown(self, kill_session=False):
        """Stop every job and refresh, then exit (and with kill_session,
        end the whole boxen tmux session, this window included)."""
        if self._shutting_down:
            return
        self._shutting_down = True
        self.workers.cancel_all()
        try:
            await self.dashboard.jobs.stop_all()
        except Exception:
            pass
        procs.kill_all()
        if kill_session and in_boxen_tmux(self.env):
            try:
                subprocess.Popen(self.dashboard.tmux.kill_session_argv(), env=self.env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError:
                pass
        self.exit()


def main():
    app = BoxenApp()
    try:
        app.run()
    finally:
        procs.kill_all()


__all__ = ["BoxenApp", "Dashboard", "editor_argv", "GLOBAL", "main"]
