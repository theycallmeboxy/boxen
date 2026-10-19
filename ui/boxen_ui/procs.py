"""Subprocesses: started with argv lists only (never a shell), each in its
own process group, and tracked so that none outlives the app."""
import asyncio
import os
import signal
import subprocess
import time

_live = set()          # asyncio.subprocess.Process
_termed = set()        # the ones stop() has sent SIGTERM, until they're reaped


def _killpg(proc, sig):
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


async def spawn(argv, env=None, merge_stderr=True, cwd=None):
    """Start argv (stdin from /dev/null, stdout piped, stderr merged or
    piped) in cwd (default: the app's). Tracked until reaped."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        env=env, cwd=cwd, start_new_session=True)
    _live.add(proc)
    return proc


def forget(proc):
    _live.discard(proc)


async def _reaped(proc, timeout):
    """Wait up to timeout for proc to exit and be reaped; True if it was.
    A cancellation ends the wait early and isn't passed on: the callers
    are cleaning up, and the caller of theirs re-raises it."""
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    return proc.returncode is not None


async def stop(proc, grace=2.0):
    """Terminate proc's process group, give it grace seconds, then kill
    it. SIGTERM goes once however many callers stop it at the same time (a
    second one could cut short the cleanup the first one started). proc
    stays tracked until it's been reaped, so kill_all still sees it if
    this is cut short."""
    if proc.returncode is None:
        if proc not in _termed:
            _termed.add(proc)
            _killpg(proc, signal.SIGTERM)
        if not await _reaped(proc, grace):
            _killpg(proc, signal.SIGKILL)
            await _reaped(proc, 1.0)
    if proc.returncode is not None:
        _termed.discard(proc)
        forget(proc)


async def run(argv, env=None, timeout=30.0, merge_stderr=False, cwd=None):
    """Run argv to completion. Returns (rc, stdout, stderr) as text;
    rc 127 when it can't be started, 124 on timeout. Cancelling the caller
    kills the process."""
    try:
        proc = await spawn(argv, env=env, merge_stderr=merge_stderr, cwd=cwd)
    except OSError as e:
        return 127, "", "%s: %s" % (argv[0], e.strerror or e)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        _killpg(proc, signal.SIGKILL)
        await proc.wait()
        forget(proc)
        return 124, "", "%s timed out after %ds" % (os.path.basename(argv[0]), timeout)
    except asyncio.CancelledError:
        _killpg(proc, signal.SIGKILL)
        if await _reaped(proc, 1.0):
            forget(proc)
        raise
    forget(proc)
    return (proc.returncode,
            (out or b"").decode("utf-8", "replace"),
            (err or b"").decode("utf-8", "replace"))


def kill_all():
    """Last resort at exit: SIGTERM every tracked process group, then
    SIGKILL whatever is left."""
    procs = [p for p in _live if p.returncode is None]
    for p in procs:
        _killpg(p, signal.SIGTERM)
    if procs:
        time.sleep(0.3)
    for p in procs:
        _killpg(p, signal.SIGKILL)
    _live.clear()
    _termed.clear()


def live():
    return [p for p in _live if p.returncode is None]
