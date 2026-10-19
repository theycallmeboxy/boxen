"""Jobs: boxen commands the UI runs, with their output as it comes.

Every job runs `boxen ...` (argv, no shell) with BOXEN_AGENT=1, so nothing
waits on a terminal: a question comes back as exit 3 with an "approval
needed" block (see bin/boxen approval_needed), which parse_approval reads.
One job per project at a time; a global job (update all, tokens) runs alone.
"""
import asyncio
import os
import re
import time
from dataclasses import dataclass, field

from . import procs
from .config import valid_name
from .textutil import clean_line

MAX_LINES = 5000
SPLIT = 65536        # a line longer than this (bytes) is shown in pieces
# In Job.raw, a piece of an over-long line starts with this: it can't be a
# header, a question line or the run line of an approval block (a block
# with a carriage return in it is refused outright).
FRAGMENT = "\r"


def utf8_cut(b, n):
    """Where to cut bytes b at most n bytes in, between UTF-8 characters."""
    cut = n
    while cut > 0 and (b[cut] & 0xC0) == 0x80:
        cut -= 1
    return cut or n


class LineSplitter:
    """Bytes in, lines out (emit(text, fragment)). Lines over SPLIT bytes
    come out in pieces cut between characters, every piece (the last one
    too) flagged as a fragment."""

    def __init__(self, emit):
        self.emit = emit
        self.buf = b""
        self.long = False

    def feed(self, chunk):
        self.buf += chunk
        while b"\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n", 1)
            raw = self._pieces(raw)
            self.emit(raw.decode("utf-8", "replace"), self.long)
            self.long = False
        self.buf = self._pieces(self.buf)

    def _pieces(self, b):
        """Emit leading SPLIT-sized pieces of b (cut between characters) as
        fragments; return what's left."""
        while len(b) > SPLIT:
            cut = utf8_cut(b, SPLIT)
            self.emit(b[:cut].decode("utf-8", "replace"), True)
            b = b[cut:]
            self.long = True
        return b

    def close(self):
        if self.buf:
            self.emit(self.buf.decode("utf-8", "replace"), self.long)
        self.buf = b""
MAX_FINISHED = 50

RUNNING, OK, FAILED, APPROVAL, STOPPED = "running", "ok", "failed", "needs approval", "stopped"

_HEAD = re.compile(r"^boxen: approval needed \(([a-z]+)\) for ([a-z0-9]+(?:[_-][a-z0-9]+)*):$")
_RUN = re.compile(r"^boxen: to approve, run: (.+)$")
# What each kind of question may be answered with, and which jobs can ask it.
_ANSWERS = {"trust": ("up", ["--approve", "trust"], ("up", "restart", "new")),
            "rm": ("rm", ["--yes"], ("rm",))}


@dataclass
class Approval:
    kind: str
    name: str
    question: list       # the question's lines, as printed (each indented by 4)
    block: list          # the whole block: header, question, run line
    command: str         # what follows "to approve, run: "
    argv: list           # command split; argv[0] == "boxen"


def parse_approval(raw, job_project, job_verb):
    """The approval block a job (`boxen <job_verb> <job_project> ...`) ended
    with, from its raw output lines (before any cleaning), or None. Taken
    only when it's exactly what bin/boxen's approval_needed prints and
    nothing else could have: the run line is the last line of the output;
    above it only lines indented by 4, up to the header; no line of the
    block holds a carriage return or an escape character (which could
    redraw what's shown); the header names the job's own project; and the
    command is the one answer for that kind of question (trust: boxen up
    NAME --approve trust, from an up/restart/new job; rm: boxen rm NAME
    --yes, from an rm job)."""
    lines = list(raw)
    while lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return None
    r = _RUN.match(lines[-1])
    if not r:
        return None
    i = len(lines) - 2
    while i >= 0 and lines[i].startswith("    "):
        i -= 1
    if i < 0:
        return None
    m = _HEAD.match(lines[i])
    block = lines[i:]
    if not m or any("\r" in x or "\x1b" in x for x in block):
        return None
    kind, name = m.group(1), m.group(2)
    if name != job_project or not valid_name(name) or kind not in _ANSWERS:
        return None
    verb, flags, asked_by = _ANSWERS[kind]
    if job_verb not in asked_by:
        return None
    cmd = r.group(1)
    if cmd != " ".join(["boxen", verb, name] + flags):
        return None
    return Approval(kind, name, block[1:-1], block, cmd, ["boxen", verb, name] + flags)


_WAITING = re.compile(r"^(?:boxen: )?([a-z0-9_-]+)(?: is waiting at an unrecognized question|: needs an answer)")


def question_project(lines, job_project):
    """The project an exit-3 job without an approval line was about: only
    ever the job's own (None if the output says otherwise)."""
    for line in reversed(lines):
        m = _WAITING.match(line)
        if m:
            return job_project if m.group(1) == job_project else None
    return job_project


@dataclass
class Job:
    id: int
    title: str           # "boxen up caulker"
    argv: list
    project: object      # project name, or None for a global job
    is_global: bool = False
    status: str = RUNNING
    rc: object = None
    lines: list = field(default_factory=list)     # cleaned, for display
    raw: list = field(default_factory=list)       # as received, for parse_approval
    started: float = field(default_factory=time.monotonic)
    ended: object = None
    approval: object = None
    proc: object = None
    dropped: int = 0     # lines dropped from the start (MAX_LINES)
    followup: object = None   # what the dashboard does when it succeeds ("apply")

    @property
    def verb(self):
        return self.argv[1] if len(self.argv) > 1 else ""

    @property
    def display(self):
        """The command without the leading "boxen"."""
        return self.title[len("boxen "):] if self.title.startswith("boxen ") else self.title

    def elapsed(self):
        end = self.ended if self.ended is not None else time.monotonic()
        return max(0.0, end - self.started)

    def needs_terminal(self):
        return self.rc == 2 and any("needs a terminal" in line for line in self.lines[-10:])


def fmt_elapsed(s):
    s = int(s)
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm%02ds" % (s // 60, s % 60)
    return "%dh%02dm" % (s // 3600, (s % 3600) // 60)


class Busy(Exception):
    pass


class JobManager:
    """Runs jobs; calls listener.job_changed(job), listener.job_line(job,
    line) and listener.job_done(job) on the app's event loop."""

    def __init__(self, boxen, listener, env=None):
        self.boxen = boxen
        self.listener = listener
        self.env = dict(os.environ if env is None else env)
        self.env["BOXEN_AGENT"] = "1"
        self.jobs = []
        self._next = 1
        self.external_global = None    # callable -> reason or None (a global command in a window)
        self.pruned = []

    def running(self):
        return [j for j in self.jobs if j.status == RUNNING]

    def busy_reason(self, project=None, is_global=False):
        """Why a new job can't start now, or None."""
        run = self.running()
        g = [j for j in run if j.is_global]
        if g:
            return "busy: %s is running" % g[0].title
        ext = self.external_global() if self.external_global else None
        if ext:
            return ext
        if is_global:
            if run:
                return "busy: %s is running (global actions wait for every job)" % run[0].title
            return None
        for j in run:
            if j.project == project:
                return "%s is busy: %s is running" % (project, j.title)
        return None

    def start(self, args, project=None, is_global=False, title=None):
        """Start `boxen <args>` as a job. Raises Busy."""
        why = self.busy_reason(project, is_global)
        if why:
            raise Busy(why)
        job = Job(self._next, title or " ".join(["boxen"] + list(args)),
                  [self.boxen] + list(args), project, is_global)
        self._next += 1
        self.jobs.append(job)
        self.pruned = self._prune()
        return job

    def _prune(self):
        """Keep at most MAX_FINISHED finished jobs (the oldest go); returns
        the ones dropped."""
        done = [j for j in self.jobs if j.status != RUNNING]
        drop = done[:max(0, len(done) - MAX_FINISHED)]
        if drop:
            ids = {j.id for j in drop}
            self.jobs = [j for j in self.jobs if j.id not in ids]
        return drop

    async def run(self, job):
        """Run a started job to its end (the app runs this in a worker)."""
        try:
            job.proc = await procs.spawn(job.argv, env=self.env)
        except OSError as e:
            self._line(job, "boxen ui: can't run %s: %s" % (job.argv[0], e.strerror or e))
            self._finish(job, 127)
            return
        self.listener.job_changed(job)
        lines = LineSplitter(lambda text, fragment: self._line(job, text, fragment))
        try:
            while True:
                chunk = await job.proc.stdout.read(4096)
                if not chunk:
                    break
                lines.feed(chunk)
            lines.close()
            rc = await job.proc.wait()
        except asyncio.CancelledError:
            await procs.stop(job.proc)
            self._finish(job, None, STOPPED)
            raise
        procs.forget(job.proc)
        self._finish(job, rc)

    def _line(self, job, raw, fragment=False):
        line = clean_line(raw)
        job.raw.append(FRAGMENT + raw if fragment else raw)
        job.lines.append(line)
        if len(job.lines) > MAX_LINES:
            n = len(job.lines) - MAX_LINES
            del job.lines[:n]
            del job.raw[:n]
            job.dropped += n
        self.listener.job_line(job, line)

    def _finish(self, job, rc, status=None):
        job.rc = rc
        job.ended = time.monotonic()
        if status:
            job.status = status
        elif rc == 0:
            job.status = OK
        elif rc == 3:
            job.approval = parse_approval(job.raw, job.project, job.verb)
            job.status = APPROVAL
        else:
            job.status = FAILED
        self.listener.job_changed(job)
        self.listener.job_done(job)

    async def stop_all(self):
        for j in self.running():
            if j.proc is not None:
                await procs.stop(j.proc, grace=1.0)
