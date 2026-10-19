"""Subprocesses: started with argv lists only (never a shell), each in its
own process group, and tracked so that none outlives the app."""
import asyncio
import os
import signal
import subprocess
import time

_live = set()          # asyncio.subprocess.Process


def _killpg(proc, sig):
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


async def spawn(argv, env=None, merge_stderr=True):
    """Start argv (stdin from /dev/null, stdout piped, stderr merged or
    piped). Tracked until reaped."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        env=env, start_new_session=True)
    _live.add(proc)
    return proc


def forget(proc):
    _live.discard(proc)


async def stop(proc, grace=2.0):
    """Terminate proc's process group, then kill it if it doesn't go."""
    if proc.returncode is None:
        _killpg(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), grace)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _killpg(proc, signal.SIGKILL)
    forget(proc)


async def run(argv, env=None, timeout=30.0, merge_stderr=False):
    """Run argv to completion. Returns (rc, stdout, stderr) as text;
    rc 127 when it can't be started, 124 on timeout. Cancelling the caller
    kills the process."""
    try:
        proc = await spawn(argv, env=env, merge_stderr=merge_stderr)
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
        try:
            await asyncio.shield(proc.wait())
        except asyncio.CancelledError:
            pass
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


def live():
    return [p for p in _live if p.returncode is None]
