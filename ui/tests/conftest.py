"""Fixtures for boxen ui's headless tests (needs textual and pytest:
python -m pytest ui/tests): fake boxen, docker, tmux and claude commands, a
fake Docker Engine API on a unix socket, a BOXEN_HOME and a ~/src."""
import asyncio
import functools
import html
import itertools
import json
import os
import re
import socketserver
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import unquote

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.dirname(HERE)
sys.path.insert(0, UI_DIR)
FAKES = os.path.join(HERE, "fakes")
# Unix socket paths are limited to 107 bytes: keep the fake Docker socket short.
SOCK_DIR = os.environ.get("BOXEN_UI_TEST_SOCKDIR") or tempfile.mkdtemp(prefix="bxs")
_seq = itertools.count()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def address_string(self):
        return "unix"

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        srv = self.server
        path = self.path.split("?", 1)[0]
        if path == "/_ping":
            return self._send(200, b"OK", "text/plain")
        if path == "/info":
            return self._send(200, json.dumps({"DockerRootDir": srv.root}).encode())
        if path == "/containers/json":
            out = []
            for name, (state, health) in srv.states.items():
                st = "Up 2 hours" + (" (%s)" % health if health else "") if state == "running" else "Exited (0)"
                out.append({"Names": ["/" + name], "State": state, "Status": st})
            return self._send(200, json.dumps(out).encode())
        m = re.match(r"^/containers/([^/]+)/stats$", path)
        if m:
            name = unquote(m.group(1))
            if name not in srv.containers:
                return self._send(404, b'{"message":"no such container"}')
            n = srv.counts.get(name, 0) + 1
            srv.counts[name] = n
            share = srv.containers[name]
            body = {"cpu_stats": {"cpu_usage": {"total_usage": int(n * 2e8 * share)},
                                  "system_cpu_usage": int(n * 1e9), "online_cpus": 4},
                    "precpu_stats": {"cpu_usage": {"total_usage": 0}},
                    "memory_stats": {"usage": 300 * 2**20 + n * 2**20, "limit": 8 * 2**30,
                                     "stats": {"inactive_file": 50 * 2**20}}}
            return self._send(200, json.dumps(body).encode())
        return self._send(404, b'{"message":"not found"}')


class FakeDocker(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path, containers, root, states):
        self.containers = containers      # running ones: name -> CPU share
        self.states = states              # every one: name -> (state, health)
        self.counts = {}
        self.root = root
        super().__init__(path, _Handler)


def project(name, state="running", status=None, sessions=0, serve="off", github="token ok", repo=None,
            sidecars=(), repos=None):
    """A project as `boxen ls --json` (version 3) lists it; state None: no
    build container yet. The path is filled in by Env.set_ls."""
    c = None
    if state is not None:
        c = {"state": state, "status": status if status is not None else
             ("Up 2 hours" if state == "running" else "Exited (0) 3 hours ago")}
    if repos is None:
        repos = [{"repo": repo, "state": "ok" if github == "token ok" else "-", "origin": True}] if repo else []
    return {"name": name, "container": c, "sidecars": list(sidecars), "sessions": sessions, "serve": serve,
            "github": github, "repo": repo, "repos": list(repos)}


def sidecar(project_name, svc, state="running", health=None):
    """A sidecar as `ls --json` lists it (Docker's status text with the
    health in it, as docker ps says it)."""
    status = ("Up 2 hours" + (" (%s)" % health if health else "")) if state == "running" else "Exited (0) 1 hour ago"
    return {"service": svc, "container": "boxen.%s.%s" % (project_name, svc), "state": state, "status": status}


def four_projects():
    return {"projects": [
        project("caulker", repo="theycallmeboxy/caulker", sessions=2, serve="on",
                sidecars=[sidecar("caulker", "postgres", health="healthy"), sidecar("caulker", "redis")]),
        project("beta", state="exited", github="-"),
        project("gamma", state=None, serve="stopped", github="token stale", repo="theycallmeboxy/gamma"),
    ], "v1": []}


class Env:
    """One test's world: env for the app, where the fakes write."""

    def __init__(self, root, ls=None, docker=True):
        self.root = root
        self.fake = os.path.join(root, "fake")
        self.bin = os.path.join(root, "bin")
        self.home = os.path.join(root, "boxen-home")
        self.src = os.path.join(root, "src")
        for d in (self.fake, self.bin, self.home, self.src, os.path.join(self.home, "projects")):
            os.makedirs(d, exist_ok=True)
        for name in ("boxen", "docker", "tmux"):
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write('#!/bin/sh\nexec "%s" "%s/fake_%s.py" "$@"\n' % (sys.executable, FAKES, name))
            os.chmod(p, 0o755)
        self.set_ls(ls if ls is not None else four_projects())
        # A tmux session always has a window: the dashboard.
        self.windows([{"id": "@0", "name": "dashboard"}])
        self.sock = os.path.join(SOCK_DIR, "d%d-%d.sock" % (os.getpid(), next(_seq)))
        self.server = None
        if docker:
            names, states = {}, {}
            for p in self.ls["projects"]:
                if p["container"]:
                    states["boxen.%s.env" % p["name"]] = (p["container"]["state"], None)
                if p["container"] and p["container"]["state"] == "running":
                    names["boxen.%s.env" % p["name"]] = 0.25 if p["name"] == "caulker" else 0.05
                for s in p["sidecars"]:
                    health = "healthy" if "(healthy)" in s["status"] else None
                    states[s["container"]] = (s["state"], health)
                    if s["state"] == "running":
                        names[s["container"]] = 0.02
            self.server = FakeDocker(self.sock, names, root, states)
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
        for p in self.ls["projects"]:
            os.makedirs(os.path.join(self.home, "projects", p["name"]), exist_ok=True)
            os.makedirs(os.path.join(self.src, p["name"], ".boxen"), exist_ok=True)
        self.env = {
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", root),
            "FAKE_DIR": self.fake,
            "BOXEN_UI_BOXEN": os.path.join(self.bin, "boxen"),
            "BOXEN_UI_TMUX": os.path.join(self.bin, "tmux"),
            "BOXEN_HOME": self.home,
            "BOXEN_SRC": self.src,
            "BOXEN_REPO": os.path.dirname(UI_DIR),
            "BOXEN_DOCKER_HOST": "unix://" + self.sock,
            "XDG_STATE_HOME": os.path.join(root, "state"),
            "TMUX": "/tmp/tmux-1000/boxen,4242,0",
            "EDITOR": "vim",
            "LANG": "C.UTF-8",
        }

    def set_ls(self, doc):
        self.ls = doc
        out = json.loads(json.dumps(doc))
        out.setdefault("version", 3)
        out.setdefault("docker", True)
        out.setdefault("src", self.src)
        out.setdefault("v1", [])
        for p in out["projects"]:
            p.setdefault("path", os.path.join(self.src, p["name"]))
        with open(os.path.join(self.fake, "ls.json"), "w") as f:
            json.dump(out, f)

    def rules(self, rules):
        with open(os.path.join(self.fake, "boxen.json"), "w") as f:
            json.dump({"rules": rules}, f)

    def write(self, name, content):
        with open(os.path.join(self.fake, name), "w") as f:
            f.write(content)

    def windows(self, wins):
        self.write("windows.json", json.dumps(wins))

    def _calls(self, name):
        p = os.path.join(self.fake, name)
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(line) for line in f if line.strip()]

    def boxen_calls(self, skip_ls=True):
        return [c for c in self._calls("boxen.jsonl")
                if not (skip_ls and (c["args"] in (["ls", "--json"], ["df", "--json"])
                                     or (c["args"][:1] == ["repos"] and c["args"][-1:] == ["--suggest"])))]

    def tmux_calls(self, cmd=None):
        out = [c["argv"] for c in self._calls("tmux.jsonl")]
        return [a for a in out if cmd is None or cmd in a]

    def docker_calls(self):
        return [c["args"] for c in self._calls("docker.jsonl")]

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            try:
                os.unlink(self.sock)
            except OSError:
                pass


@pytest.fixture
def world(tmp_path):
    envs = []

    def make(**kw):
        e = Env(str(tmp_path / ("w%d" % len(envs))), **kw)
        envs.append(e)
        return e
    yield make
    for e in envs:
        e.close()


def aio(fn):
    """Run an async test on its own event loop (no pytest plugin needed)."""
    @functools.wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


def screen_text(app):
    """Everything on screen, as plain text (from the SVG screenshot)."""
    svg = app.export_screenshot()
    parts = re.findall(r"<text[^>]*>(.*?)</text>", svg, flags=re.S)
    return html.unescape("".join(parts)).replace("\xa0", " ")


async def wait_for(pilot, cond, timeout=8.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError("timed out waiting for %s" % what)
