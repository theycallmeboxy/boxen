"""Jobs: boxen commands the UI runs, with their output as it comes.

Every job runs `boxen ...` (argv, no shell) with stdin from /dev/null, so
nothing can wait on a terminal: the dashboard only runs commands that don't
ask (rm with --yes, after its own question), and opens a tmux window for
the ones that need a terminal. One job per project at a time; a global job
(update, tokens, doctor) runs alone.
"""
import asyncio
import os
import time
from dataclasses import dataclass, field

from . import procs
from .textutil import clean_line

MAX_LINES = 5000
SPLIT = 65536        # a line longer than this (bytes) is shown in pieces


def utf8_cut(b, n):
    """Where to cut bytes b at most n bytes in, between UTF-8 characters."""
    cut = n
    while cut > 0 and (b[cut] & 0xC0) == 0x80:
        cut -= 1
    return cut or n


class LineSplitter:
    """Bytes in, lines out (emit(text)). Lines over SPLIT bytes come out in
    pieces cut between characters, so one huge line can't stall the log."""

    def __init__(self, emit):
        self.emit = emit
        self.buf = b""

    def feed(self, chunk):
        self.buf += chunk
        while b"\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n", 1)
            self.emit(self._pieces(raw).decode("utf-8", "replace"))
        self.buf = self._pieces(self.buf)

    def _pieces(self, b):
        """Emit leading SPLIT-sized pieces of b (cut between characters);
        return what's left."""
        while len(b) > SPLIT:
            cut = utf8_cut(b, SPLIT)
            self.emit(b[:cut].decode("utf-8", "replace"))
            b = b[cut:]
        return b

    def close(self):
        if self.buf:
            self.emit(self.buf.decode("utf-8", "replace"))
        self.buf = b""


MAX_FINISHED = 50

RUNNING, OK, FAILED, STOPPED = "running", "ok", "failed", "stopped"


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
    started: float = field(default_factory=time.monotonic)
    ended: object = None
    proc: object = None
    dropped: int = 0     # lines dropped from the start (MAX_LINES)
    then: object = None  # called with the job (on the app's loop) when it succeeds

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
        lines = LineSplitter(lambda text: self._line(job, text))
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

    def _line(self, job, raw):
        line = clean_line(raw)
        job.lines.append(line)
        if len(job.lines) > MAX_LINES:
            n = len(job.lines) - MAX_LINES
            del job.lines[:n]
            job.dropped += n
        self.listener.job_line(job, line)

    def _finish(self, job, rc, status=None):
        job.rc = rc
        job.ended = time.monotonic()
        if status:
            job.status = status
        elif rc == 0:
            job.status = OK
        else:
            job.status = FAILED
        self.listener.job_changed(job)
        self.listener.job_done(job)

    async def stop_all(self):
        for j in self.running():
            if j.proc is not None:
                await procs.stop(j.proc, grace=1.0)
