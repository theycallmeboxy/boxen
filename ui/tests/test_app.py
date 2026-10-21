"""Headless tests of boxen ui (Textual Pilot) against fake boxen, docker,
tmux and Docker Engine API. Run: python -m pytest ui/tests"""
import json
import os
import time

import pytest
from conftest import aio, four_projects, project, screen_text, sidecar, wait_for

from boxen_ui import procs
from boxen_ui.edit import spec_sha
from boxen_ui.app import CLAUDE_ARGV, BoxenApp, editor_argv
from boxen_ui.model import parse_ls
from boxen_ui.screens import ConfirmScreen, GlobalMenuScreen, NewProjectScreen, ReposScreen
from boxen_ui.tmux import hold_script

SIZE = (120, 35)
INJECT_LINK = "[link=file:///etc/passwd]x[/link]"
INJECT_CLICK = "[@click=app.quit]boom[/]"


def gone(pid, timeout=3.0):
    """Wait for pid to have exited: a signalled process takes a moment."""
    end = time.monotonic() + timeout
    while alive(pid):
        assert time.monotonic() < end, "pid %d still running" % pid
        time.sleep(0.02)


def alive(pid):
    """Running (a zombie waiting to be reaped doesn't count)."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


async def started(pilot, app):
    await wait_for(pilot, lambda: app.dashboard.ls_loaded and app.dashboard.selected, what="first ls")
    await pilot.pause(0.1)          # let the first rows paint


def rows(app, table_id):
    t = app.dashboard.query_one(table_id)
    return [k.value for k in t.rows]


async def press_button(pilot, app, sel):
    """Activate a dialog button by keyboard. A Button ignores presses for
    0.2 s after the last one, so wait that out first."""
    app.screen.query_one(sel).focus()
    await pilot.pause(0.25)
    assert app.focused is app.screen.query_one(sel)
    await pilot.press("enter")


async def job_done(pilot, app, n=1):
    await wait_for(pilot, lambda: len(app.dashboard.jobs.jobs) >= n and not app.dashboard.jobs.running(),
                   what="jobs to finish")


def new_window(w, i=0):
    """The i-th new-window call as (name, start folder, script)."""
    nw = w.tmux_calls("new-window")[i]
    assert nw[:5] == ["-L", "boxen", "new-window", "-t", "=boxen:"] and nw[-3:-1] == ["sh", "-c"]
    return nw[nw.index("-n") + 1], nw[nw.index("-c") + 1], nw[-1]


@aio
async def test_list_renders(world):
    w = world()
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        assert rows(app, "#projects") == ["caulker", "beta", "gamma"]
        want = ("caulker", "running", "exited", "stopped", "stale", "SESS", "SERVE")
        await wait_for(pilot, lambda: all(x in screen_text(app) for x in want), what="the list on screen")
        t = app.dashboard.query_one("#projects")
        assert [t.get_cell(k, "serve").plain for k in ("caulker", "beta", "gamma")] == ["on", "-", "stopped"]
        assert [t.get_cell(k, "sess").plain for k in ("caulker", "beta", "gamma")] == ["2", "-", "-"]
        assert t.get_cell("gamma", "ctr").plain == "-"            # no build container yet
        # docker stats arrive (fake API): CPU/memory for the running build container
        await wait_for(pilot, lambda: app.dashboard.stats.get("boxen.caulker.env", {}).get("cpu") is not None,
                       what="cpu stats")
        assert app.dashboard.stats["boxen.caulker.env"]["mem"] > 0
        assert "docker up" in screen_text(app)
        assert "agent" not in screen_text(app) and "supervisor" not in screen_text(app)
        assert w.docker_calls() == []


@aio
async def test_overview_and_selection(world):
    w = world()
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        assert app.dashboard.selected == "caulker"
        txt = screen_text(app)
        assert "2 Claude processes in the folder" in txt
        assert "on: start sessions from the Claude app" in txt
        assert str(app.dashboard.query_one("#ov-folder").render()) == os.path.join(w.src, "caulker")
        assert "Up 2 hours" in txt and "boxen.caulker.env" in txt
        assert "postgres" in txt and "healthy" in txt and "redis" in txt
        await pilot.press("down")
        assert app.dashboard.selected == "beta"
        await pilot.press("j")
        assert app.dashboard.selected == "gamma"
        txt = screen_text(app)
        assert "not created" in txt and "on, but its window isn't running" in txt and "none running" in txt
        await pilot.press("k")
        assert app.dashboard.selected == "beta"
        assert "off" in screen_text(app)


@aio
async def test_up_runs_job(world):
    w = world()
    w.rules([{"args": ["up", "caulker"], "out": ["caulker: build container up"], "delay": 0.05}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("u")
        await job_done(pilot, app)
        calls = w.boxen_calls()
        assert calls[0]["args"] == ["up", "caulker"]
        job = app.dashboard.jobs.jobs[0]
        assert job.status == "ok" and job.lines == ["caulker: build container up"]
        assert rows(app, "#job-table") == ["1"]
        await pilot.press("J")
        assert "caulker: build container up" in screen_text(app)


@aio
async def test_streaming_output_and_failure(world):
    w = world()
    w.rules([{"args": ["up", "caulker"], "out": ["one", "two", "three", "boxen: building caulker failed:"],
              "delay": 0.3, "rc": 1}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("J", "u")
        await wait_for(pilot, lambda: app.dashboard.jobs.jobs and app.dashboard.jobs.jobs[0].lines[:1] == ["one"],
                       what="first line")
        assert app.dashboard.jobs.jobs[0].status == "running"
        assert "one" in screen_text(app)
        await job_done(pilot, app)
        assert app.dashboard.jobs.jobs[0].status == "failed"
        assert "failed" in screen_text(app)


@aio
async def test_down_and_rm_ask_first(world):
    w = world()
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("d")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="down asks")
        await pilot.press("n")
        await pilot.press("X")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="rm asks")
        assert "compose.override.yaml" in screen_text(app) and "Claude history stay" in screen_text(app)
        assert app.focused is app.screen.query_one("#no")
        await pilot.press("n")
        await pilot.pause(0.3)
        assert w.boxen_calls() == []
        await pilot.press("d")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="down asks")
        await pilot.press("y")
        await job_done(pilot, app)
        await pilot.press("X")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="rm asks")
        await pilot.press("y")
        await job_done(pilot, app, 2)
        assert [c["args"] for c in w.boxen_calls()] == [["down", "caulker"], ["rm", "caulker", "--yes"]]


@aio
async def test_claude_session_windows(world):
    w = world()
    app = BoxenApp(env=w.env)
    folder = os.path.join(w.src, "caulker")
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        # no session yet: c starts one, in the project folder, named like the project
        await pilot.press("c")
        await wait_for(pilot, lambda: w.tmux_calls("new-window"), what="claude window")
        assert new_window(w) == ("caulker", folder, hold_script(CLAUDE_ARGV))
        assert CLAUDE_ARGV[:3] == ["env", "-u", "DOCKER_HOST"]          # no way to Docker from the session
        # now there is one: c switches to it
        await pilot.press("c")
        await wait_for(pilot, lambda: w.tmux_calls("select-window"), what="select-window")
        assert w.tmux_calls("select-window")[0][-2:] == ["-t", "=boxen:@1"]
        assert len(w.tmux_calls("new-window")) == 1
        # C starts another beside it
        await pilot.press("C")
        await wait_for(pilot, lambda: len(w.tmux_calls("new-window")) == 2, what="second session")
        assert new_window(w, 1) == ("caulker", folder, hold_script(CLAUDE_ARGV))
        # c picks the last one started
        await pilot.press("c")
        await wait_for(pilot, lambda: len(w.tmux_calls("select-window")) == 2, what="select the last")
        assert w.tmux_calls("select-window")[1][-1] == "=boxen:@2"
        assert w.boxen_calls() == [] and w.docker_calls() == []


@aio
async def test_claude_refuses_a_folder_that_isnt_one(world, tmp_path):
    w = world()
    folder = os.path.join(w.src, "caulker")
    os.rename(folder, folder + ".real")
    os.symlink(str(tmp_path), folder)
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("c")
        await pilot.pause(0.3)
        await pilot.press("v")
        await pilot.pause(0.3)
        assert w.tmux_calls("new-window") == []


@aio
async def test_shell_windows(world):
    w = world()
    w.env["SHELL"] = "/bin/zsh"
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("s")
        await wait_for(pilot, lambda: w.tmux_calls("new-window"), what="shell window")
        assert new_window(w) == ("caulker/shell", app.cfg.home,
                                 hold_script([w.env["BOXEN_UI_BOXEN"], "shell", "caulker"]))
        # s again: the running window is selected, not opened again
        await pilot.press("s")
        await wait_for(pilot, lambda: w.tmux_calls("select-window"), what="select")
        assert w.tmux_calls("select-window")[0][-1] == "=boxen:@1"
        # v: a shell on the VM, in the project folder
        await pilot.press("v")
        await wait_for(pilot, lambda: len(w.tmux_calls("new-window")) == 2, what="vm shell")
        assert new_window(w, 1) == ("caulker/vm-shell", os.path.join(w.src, "caulker"),
                                    hold_script(["env", "-u", "DOCKER_HOST", "GIT_CONFIG_PARAMETERS='core.fsmonitor='",
                                                 "/bin/zsh"]))


@aio
async def test_serve_on_and_off(world):
    w = world()
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("S")                          # caulker: on -> asks to turn it off
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="serve off asks")
        assert "caulker~serve" in screen_text(app)
        await pilot.press("y")
        await job_done(pilot, app)
        await pilot.press("down", "S")                  # beta: off -> on, no question
        await job_done(pilot, app, 2)
        await pilot.press("down", "S")                  # gamma: stopped -> started again
        await job_done(pilot, app, 3)
        assert [c["args"] for c in w.boxen_calls()] == [["serve", "caulker", "--off"], ["serve", "beta"],
                                                         ["serve", "gamma"]]


@aio
async def test_new_project_validation(world):
    w = world()
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("n")
        await wait_for(pilot, lambda: isinstance(app.screen, NewProjectScreen), what="new dialog")
        for bad in ("Bad", "a--b", "all", "dashboard", "-x", "x" * 41):
            app.screen.query_one("#name").value = bad
            await press_button(pilot, app, "#create")
            assert isinstance(app.screen, NewProjectScreen), bad
            assert "Name:" in str(app.screen.query_one("#error").render())
        app.screen.query_one("#name").value = "newproj"
        for bad in ("git@github.com:a/b.git", "https://gitlab.com/a/b", "https://github.com/a/../b",
                    "https://github.com/a/b/c", "http://github.com/a/b"):
            app.screen.query_one("#url").value = bad
            await press_button(pilot, app, "#create")
            assert isinstance(app.screen, NewProjectScreen), bad
            assert "URL:" in str(app.screen.query_one("#error").render())
        app.screen.query_one("#url").value = "https://github.com/theycallmeboxy/newproj.git"
        await press_button(pilot, app, "#create")
        await job_done(pilot, app)
        assert w.boxen_calls()[0]["args"] == ["new", "newproj", "https://github.com/theycallmeboxy/newproj.git"]


@aio
async def test_markup_injection_renders_literally(world):
    w = world()
    doc = four_projects()
    doc["projects"][0]["container"]["status"] = "Up " + INJECT_CLICK
    doc["projects"].append(project("[bold]x[/bold]", state=None, github=INJECT_LINK))
    w.set_ls(doc)
    w.rules([
        {"args": ["env", "logs", "postgres", "--tail", "300"],
         "out": ["log: " + INJECT_LINK, "log: " + INJECT_CLICK,
                 "\x1b]8;;file:///etc/passwd\x1b\\osc\x1b]8;;\x1b\\ \x1b[31mred\x1b[0m"]},
        {"args": ["up", "caulker"], "out": ["job: " + INJECT_LINK, "job: " + INJECT_CLICK]},
    ])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.pause(0.2)
        txt = screen_text(app)
        assert "[bold]x[/bold]" in txt              # project name in the list, literal
        assert INJECT_CLICK[:20] in txt             # container status (Overview), literal
        await pilot.press("l")
        await wait_for(pilot, lambda: "log: " in screen_text(app), what="logs")
        txt = screen_text(app)
        assert "log: " + INJECT_LINK in txt and "log: " + INJECT_CLICK in txt
        assert "osc red" in txt                     # escapes stripped, text kept
        await pilot.press("J", "u")
        await job_done(pilot, app)
        await pilot.pause(0.1)
        txt = screen_text(app)
        assert "job: " + INJECT_LINK in txt and "job: " + INJECT_CLICK in txt
        # the [@click=app.quit] strings did nothing: still running, still the dashboard
        assert app.is_running and app.screen is app.dashboard


@aio
async def test_sessions_tab_lists_the_projects_windows(world):
    w = world()
    w.windows([{"id": "@0", "name": "dashboard"}, {"id": "@3", "name": "caulker"},
               {"id": "@4", "name": "caulker~serve"}, {"id": "@5", "name": "caulker/shell", "done": True},
               {"id": "@6", "name": "other/shell"}, {"id": "@7", "name": "caulker-two"},
               {"id": "@8", "name": "caulker-two~serve"}])
    w.write("capture.txt", "$ ls\n\x1b[32mgreen.txt\x1b[0m " + INJECT_LINK + "\n"
            "\x1b]8;;file:///etc/passwd\x1b\\click\x1b]8;;\x1b\\\x1b]0;title\x07\n")
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await wait_for(pilot, lambda: "1 window here" in str(app.dashboard.query_one("#ov-sessions").render()),
                       what="Overview counts the running sessions")
        app.dashboard.action_sessions_tab()
        await wait_for(pilot, lambda: rows(app, "#win-list") == ["@3", "@4", "@5"], what="window list")
        t = app.dashboard.query_one("#win-list")
        assert [t.get_cell(k, "what").plain for k in ("@3", "@4", "@5")] == [
            "Claude session", "Remote Control server", "build container shell"]
        assert t.get_cell("@5", "state").plain == "exited"
        await wait_for(pilot, lambda: "green.txt" in screen_text(app), what="preview")
        txt = screen_text(app)
        assert INJECT_LINK in txt and "click" in txt and "title" not in txt
        prev = app.dashboard.query_one("#win-preview").render()
        assert not any(getattr(s.style, "link", None) for s in prev.spans)
        t.focus()
        await pilot.press("enter")
        await wait_for(pilot, lambda: w.tmux_calls("select-window"), what="select")
        assert w.tmux_calls("select-window")[0][-1] == "=boxen:@3"
        # x closes the selected window, after asking
        await pilot.press("x")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="close asks")
        assert "claude --resume" in screen_text(app)
        await pilot.press("y")
        await wait_for(pilot, lambda: w.tmux_calls("kill-window"), what="kill-window")
        assert w.tmux_calls("kill-window")[0] == ["-L", "boxen", "kill-window", "-t", "@3"]
        await wait_for(pilot, lambda: rows(app, "#win-list") == ["@4", "@5"], what="list updated")


@aio
async def test_logs_tab_shows_sidecars_through_boxen_env(world):
    w = world()
    w.rules([{"args": ["env", "logs", "postgres", "--tail", "300"], "out": ["pg ready"]},
             {"args": ["env", "logs", "redis", "--tail", "300"], "out": ["redis ready"]}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("l")
        await wait_for(pilot, lambda: "pg ready" in screen_text(app), what="postgres log")
        call = w.boxen_calls()[0]
        assert call["args"] == ["env", "logs", "postgres", "--tail", "300"]
        assert call["cwd"] == os.path.join(w.src, "caulker")     # boxen env acts on the folder it runs in
        app.dashboard.query_one("#log-source").value = "redis"
        await wait_for(pilot, lambda: "redis ready" in screen_text(app), what="redis log")
        await pilot.press("f")
        await wait_for(pilot, lambda: w.tmux_calls("new-window"), what="follow window")
        name, cwd, script = new_window(w)
        assert name == "caulker/logs-redis" and cwd == os.path.join(w.src, "caulker")
        assert "env logs redis -f --tail 100" in script
        await pilot.press("down")                            # beta: no sidecars
        await wait_for(pilot, lambda: "No sidecars" in screen_text(app), what="no sidecars")
        assert w.docker_calls() == []


@aio
async def test_docker_down(world):
    w = world(docker=False)
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await wait_for(pilot, lambda: "docker down" in screen_text(app), what="docker down in header")
        await pilot.press("u")
        await job_done(pilot, app)
        assert w.boxen_calls()[0]["args"] == ["up", "caulker"]


@aio
async def test_ls_failure_and_zero_projects(world):
    w = world(ls={"projects": []})
    w.write("ls.fail", "")
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await wait_for(pilot, lambda: app.dashboard.ls_loaded, what="ls")
        await pilot.pause(0.1)
        assert "boxen ls failed: boxen: something broke in ls" in screen_text(app)
        os.unlink(os.path.join(w.fake, "ls.fail"))
        app.dashboard.refresh_ls()
        await wait_for(pilot, lambda: app.dashboard.ls_error is None, what="ls recovers")
        await pilot.pause(0.1)
        txt = screen_text(app)
        assert "No projects yet" in txt and "ls failed" not in txt
        await pilot.press("u", "c", "S")          # nothing selected: no crash, nothing run
        await pilot.pause(0.2)
        assert w.boxen_calls() == [] and w.tmux_calls("new-window") == []


@aio
async def test_ls_other_version_is_an_error(world):
    w = world()
    doc = four_projects()
    doc["version"] = 2
    w.set_ls(doc)
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await wait_for(pilot, lambda: app.dashboard.ls_loaded, what="ls")
        await pilot.pause(0.1)
        assert "version 2; this dashboard reads 3" in screen_text(app)
        assert app.dashboard.projects == []


@aio
async def test_v1_projects_banner(world):
    w = world()
    doc = four_projects()
    doc["v1"] = ["oldproj", INJECT_CLICK]
    w.set_ls(doc)
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await wait_for(pilot, lambda: app.dashboard.query_one("#banner").has_class("shown"), what="banner")
        txt = str(app.dashboard.query_one("#banner").render())
        assert "boxen v1 projects not moved yet: oldproj, " + INJECT_CLICK in txt and "boxen migrate" in txt
        assert rows(app, "#projects") == ["caulker", "beta", "gamma"]     # not listed as projects
        app.dashboard.action_global_migrate()
        await wait_for(pilot, lambda: w.tmux_calls("new-window"), what="migrate window")
        assert new_window(w)[0] == "+global/migrate"
        assert new_window(w)[2] == hold_script([w.env["BOXEN_UI_BOXEN"], "migrate"])


def test_parse_ls_is_defensive():
    doc = {"version": 3, "docker": False, "src": "/s", "v1": ["a", 3, None],
           "projects": [{"name": "a", "container": {"state": 5}, "sidecars": [{"service": "db"}, {"x": 1}, "s"],
                         "sessions": True, "serve": "maybe", "github": None, "repos": [{"repo": ""}, "x"]},
                        {"name": "b", "path": "relative", "sessions": -1},
                        {"name": ""}, "junk", {"name": 7}]}
    ls = parse_ls(json.dumps(doc), "/other")
    assert [p.name for p in ls.projects] == ["a", "b"] and ls.docker is False and ls.v1 == ["a"]
    a, b = ls.projects
    assert a.container.state == "?" and a.sessions == 0 and a.serve == "off" and a.github == "-"
    assert [(s.service, s.container, s.state) for s in a.sidecars] == [("db", "boxen.a.db", "?")]
    assert a.repos == [] and a.path == "/s/a" and b.path == "/s/b" and b.sessions == 0 and b.container is None
    for bad in ('[]', '{"projects": {}}', '{"version": 2, "projects": []}', '{"projects": []}'):
        with pytest.raises(ValueError):
            parse_ls(bad, "/s")


@aio
async def test_symlinked_build_files_are_refused(world, tmp_path):
    w = world()
    secret = tmp_path / "app.pem"
    secret.write_text("-----BEGIN PRIVATE KEY----- SECRET")
    bdir = os.path.join(w.src, "caulker", ".boxen")
    os.symlink(str(secret), os.path.join(bdir, "Dockerfile"))
    # beta's whole .boxen is a link to somewhere else
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "Dockerfile").write_text("FROM evil\n")
    os.rmdir(os.path.join(w.src, "beta", ".boxen"))
    os.symlink(str(other), os.path.join(w.src, "beta", ".boxen"))
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        app.dashboard.action_files_tab()
        await wait_for(pilot, lambda: rows(app, "#files"), what="files")
        cell = lambda: app.dashboard.query_one("#files").get_cell(".boxen/Dockerfile", "state").plain  # noqa: E731
        assert cell() == "refused: .boxen/Dockerfile is a symlink"
        assert "SECRET" not in screen_text(app)
        await pilot.press("e")                      # the first row: .boxen/Dockerfile
        await pilot.pause(0.3)
        assert w.tmux_calls("new-window") == []
        await pilot.press("down")
        await wait_for(pilot, lambda: cell() == "refused: .boxen/ is a symlink or not a folder", what="beta's refusal")
        await pilot.press("e")
        await pilot.pause(0.3)
        assert w.tmux_calls("new-window") == []


@aio
async def test_edit_file_opens_a_private_copy(world):
    w = world()
    bdir = os.path.join(w.src, "caulker", ".boxen")
    with open(os.path.join(bdir, "Dockerfile"), "w") as f:
        f.write("FROM boxen-env:latest\n")
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        app.dashboard.action_files_tab()
        await pilot.pause(0.2)
        txt = screen_text(app)
        assert ".boxen/services.yaml" in txt and "compose.override.yaml" in txt and "repos" in txt
        await pilot.press("e")
        await wait_for(pilot, lambda: w.tmux_calls("new-window"), what="editor window")
        name, cwd, script = new_window(w)
        assert name == "caulker/edit-Dockerfile" and cwd == app.cfg.home
        spec_path, sha = script.split("boxen_ui.edit ")[1].split(";")[0].split()
        spec = json.load(open(spec_path))
        assert sha == spec_sha(spec_path)                     # the window checks spec.json against this
        # the editor never sees the real path: a private copy, mode 0600 in a 0700 folder
        assert spec["target"] == os.path.join(bdir, "Dockerfile") and spec["copy"] != spec["target"]
        assert spec["base"] == os.path.join(w.src, "caulker") and spec["rel"] == [".boxen", "Dockerfile"]
        assert open(spec["copy"]).read() == "FROM boxen-env:latest\n"
        assert os.stat(spec["dir"]).st_mode & 0o777 == 0o700
        assert spec["editor"] == ["vim"]
        assert "-P -m boxen_ui.edit" in script


async def _edit_and_save(pilot, app, w, row, content):
    """Open the Files tab's row in the editor, then do what its window does
    when vim exits, with content as the edit. Returns the spec."""
    from boxen_ui import edit
    app.dashboard.action_files_tab()
    await pilot.pause(0.2)
    app.dashboard.query_one("#files").focus()
    app.dashboard.query_one("#files").move_cursor(row=row)
    await pilot.pause(0.1)
    await pilot.press("e")
    await wait_for(pilot, lambda: app.dashboard.pending_edits, what="edit pending")
    spec = list(app.dashboard.pending_edits.values())[0]
    with open(spec["copy"], "rb") as f:
        new = content(f.read())
    with open(spec["copy"], "wb") as f:
        f.write(new)
    res = edit.write_back(spec, open(spec["copy"], "rb").read())
    assert res["status"] == "written", res
    with open(spec["result"], "w") as f:
        json.dump(res, f)
    return spec


@aio
async def test_edit_services_yaml_offers_apply(world):
    w = world()
    path = os.path.join(w.src, "caulker", ".boxen", "services.yaml")
    with open(path, "w") as f:
        f.write("services: {}\n")
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        spec = await _edit_and_save(pilot, app, w, 1, lambda old: b"services:\n  redis:\n    image: redis:7\n")
        assert spec["label"] == ".boxen/services.yaml"
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="apply offer")
        assert open(path).read().startswith("services:\n  redis")
        await pilot.press("y")
        await job_done(pilot, app)
        assert w.boxen_calls()[0]["args"] == ["up", "caulker"]
        assert not os.path.exists(spec["dir"])                 # the private copy is gone


@aio
async def test_edit_override_starts_from_the_example(world):
    w = world()
    pdir = os.path.join(w.home, "projects", "caulker")
    with open(os.path.join(pdir, "compose.override.example.yaml"), "w") as f:
        f.write("# Example for caulker\n# services:\n#   env:\n")
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        app.dashboard.action_files_tab()
        await wait_for(pilot, lambda: rows(app, "#files"), what="files")
        t = app.dashboard.query_one("#files")
        assert t.get_cell("compose.override.yaml", "state").plain == "(new, from the example)"
        assert t.get_cell("repos", "state").plain == "(new)"
        spec = await _edit_and_save(pilot, app, w, 2, lambda old: old.replace(b"# services:", b"services:"))
        assert spec["label"] == "compose.override.yaml" and spec["orig_sha"] is None, spec
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="apply offer")
        assert open(os.path.join(pdir, "compose.override.yaml")).read() == "# Example for caulker\nservices:\n#   env:\n"
        await pilot.press("n")
        await pilot.pause(0.2)
        assert w.boxen_calls() == []


@aio
async def test_edit_repos_mints_tokens(world):
    w = world()
    path = os.path.join(w.home, "projects", "caulker", "repos")
    with open(path, "w") as f:
        f.write("# repos\ntheycallmeboxy/caulker\n")
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await _edit_and_save(pilot, app, w, 3, lambda old: old + b"theycallmeboxy/lib\n")
        await job_done(pilot, app)
        assert w.boxen_calls()[0]["args"] == ["tokens", "caulker"]
        assert open(path).read().endswith("theycallmeboxy/lib\n")


def test_editor_choice(tmp_path):
    assert editor_argv({"EDITOR": "nvim -u NONE", "PATH": ""}) == ["nvim", "-u", "NONE"]
    b = tmp_path / "bin"
    b.mkdir()
    assert editor_argv({"PATH": str(b)}) == ["vi"]
    (b / "vim").write_text("#!/bin/sh\n")
    (b / "vim").chmod(0o755)
    assert editor_argv({"PATH": str(b)}) == ["vim"]


@aio
async def test_global_job_blocks_project_actions(world):
    w = world()
    w.rules([{"args": ["update"], "out": ["building the base image...", "caulker: build container up"],
              "delay": 0.8}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("g")
        await wait_for(pilot, lambda: isinstance(app.screen, GlobalMenuScreen), what="global menu")
        await pilot.press("enter")              # first option: update everything
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="confirm")
        await pilot.press("y")
        await wait_for(pilot, lambda: app.dashboard.jobs.running(), what="global job")
        await pilot.press("u", "X", "S")
        await pilot.pause(0.2)
        assert [c["args"] for c in w.boxen_calls()] == [["update"]] and app.screen is app.dashboard
        await job_done(pilot, app)
        await pilot.press("u")
        await job_done(pilot, app, 2)
        assert [c["args"] for c in w.boxen_calls()] == [["update"], ["up", "caulker"]]


@aio
async def test_doctor_is_a_global_job(world):
    w = world()
    w.rules([{"args": ["doctor"], "out": ["ok   rootless Docker", "ok   sandbox"]}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        app.dashboard.action_global_doctor()
        await job_done(pilot, app)
        assert w.boxen_calls()[0]["args"] == ["doctor"] and app.dashboard.jobs.jobs[0].is_global
        assert "ok   sandbox" in screen_text(app)          # the jobs panel opened to show it


@aio
async def test_one_job_per_project(world):
    w = world()
    w.rules([{"args": ["up", "caulker"], "out": ["slow"], "sleep": 1.0}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("u", "S")
        await pilot.press("down", "u")          # another project: allowed
        await job_done(pilot, app, 2)
        assert sorted(c["args"][1] for c in w.boxen_calls()) == ["beta", "caulker"]
        assert ["serve", "caulker", "--off"] not in [c["args"] for c in w.boxen_calls()]


@aio
async def test_q_detaches(world):
    w = world()
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("q")
        await wait_for(pilot, lambda: w.tmux_calls("detach-client"), what="detach")
        # only this client: the one the dashboard is showing on
        assert w.tmux_calls("detach-client")[0] == ["-L", "boxen", "detach-client", "-t", "/dev/pts/7"]
        assert app.is_running


@aio
async def test_Q_stops_jobs_and_leaves_the_sessions(world):
    w = world()
    w.rules([{"args": ["up", "caulker"], "out": ["working"], "sleep": 30}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("u")
        await wait_for(pilot, lambda: app.dashboard.jobs.jobs and app.dashboard.jobs.jobs[0].lines, what="job")
        pid = w.boxen_calls()[0]["pid"]
        await pilot.press("Q")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="confirm")
        assert "Claude sessions, serve and the other windows keep running" in str(app.screen._message)
        await pilot.press("y")
        await wait_for(pilot, lambda: not app.is_running, what="exit", timeout=10)
    gone(pid)
    assert procs.live() == []
    assert w.tmux_calls("kill-session") == [] and w.tmux_calls("kill-window") == []


@aio
async def test_clean_exit_kills_running_jobs(world):
    w = world()
    w.rules([{"args": ["up", "caulker"], "out": ["working"], "sleep": 30}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("u")
        await wait_for(pilot, lambda: app.dashboard.jobs.jobs and app.dashboard.jobs.jobs[0].lines, what="job")
        pid = w.boxen_calls()[0]["pid"]
    procs.kill_all()       # what main() does after the app returns
    gone(pid)
    assert procs.live() == []


@aio
async def test_command_palette_lists_actions(world):
    w = world()
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        titles = [c.title for c in app.get_system_commands(app.dashboard)]
        for t in ("Up: caulker", "Remove: caulker", "Claude: caulker", "New Claude session: caulker",
                  "Serve on/off: caulker", "VM shell: caulker", "GitHub repos: caulker", "Add found repo: caulker",
                  "Self-update boxen", "Check this VM", "Move from boxen v1"):
            assert t in titles, t
        for gone in ("Restart: caulker", "Update: caulker", "Set up the supervisor", "Sign in to Claude"):
            assert gone not in titles, gone
        await pilot.press("ctrl+p")
        await pilot.pause(0.3)
        assert "CommandPalette" in type(app.screen).__name__


@aio
async def test_self_update_window_blocks_jobs(world):
    w = world()
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        app.dashboard.action_global_self_update()
        await wait_for(pilot, lambda: w.tmux_calls("new-window"), what="self-update window")
        lock = app.cfg.global_lock
        assert new_window(w) == ("+global/self-update", app.cfg.home,
                                 hold_script([w.env["BOXEN_UI_BOXEN"], "self-update"], lock=lock))
        open(lock, "w").close()                  # the window's script is running
        await wait_for(pilot, lambda: any(x.name == "+global/self-update" for x in app.dashboard.windows),
                       what="window seen")
        await pilot.press("u")
        await pilot.pause(0.3)
        assert w.boxen_calls() == []
        os.unlink(lock)
        await pilot.press("u")
        await job_done(pilot, app)
        assert w.boxen_calls()[0]["args"] == ["up", "caulker"]


@aio
async def test_narrow_terminal(world):
    w = world()
    app = BoxenApp(env=w.env)
    async with app.run_test(size=(90, 28)) as pilot:
        await started(pilot, app)
        cols = [c.value for c in app.dashboard.query_one("#projects").columns]
        assert cols == ["name", "ctr", "sess", "serve", "cpu", "mem"]
        assert "caulker" in screen_text(app)


@aio
async def test_refresh_keys(world):
    w = world()
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        n = lambda: len([c for c in w.boxen_calls(skip_ls=False) if c["args"] == ["ls", "--json"]])  # noqa: E731
        before = n()
        await pilot.press("ctrl+r")
        await wait_for(pilot, lambda: n() > before, what="ctrl+r refresh")
        before = n()
        await pilot.press("f5")
        await wait_for(pilot, lambda: n() > before, what="F5 refresh")
        assert "R" not in [b.key for b in app.dashboard.BINDINGS]


@aio
async def test_finished_window_is_replaced(world):
    w = world()
    w.windows([{"id": "@0", "name": "dashboard"}, {"id": "@4", "name": "caulker/shell", "done": True}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("s")
        await wait_for(pilot, lambda: w.tmux_calls("new-window"), what="new window")
        assert ["-L", "boxen", "kill-window", "-t", "@4"] in w.tmux_calls("kill-window")
        assert w.tmux_calls("select-window") == []


def test_hold_script_marks_done_and_survives_ctrl_c():
    s = hold_script(["boxen", "shell", "a b"])
    assert s.startswith("trap : INT; boxen shell 'a b'; rc=$?; ")
    assert 'tmux set-option -w -t "$TMUX_PANE" @boxen_done 1' in s
    assert s.index("@boxen_done") < s.index("read -r _")


def test_window_kinds():
    from boxen_ui.tmux import kind_of
    assert kind_of("a", "a") == "claude" and kind_of("a", "a~serve") == "serve"
    assert kind_of("a", "a/shell") == "shell" and kind_of("a", "a/edit-repos") == "edit-repos"
    for other in ("ab", "a-b", "a-b~serve", "ab/shell", "+global/a", "dashboard", "b~serve"):
        assert kind_of("a", other) is None, other


@aio
async def test_polling_slows_when_nobody_looks(world, monkeypatch):
    import boxen_ui.app as appmod
    monkeypatch.setattr(appmod, "LS_ACTIVE", 1)
    monkeypatch.setattr(appmod, "LS_IDLE", 30)
    monkeypatch.setattr(appmod, "STATS_EVERY", 1)
    w = world()
    app = BoxenApp(env=w.env)
    ls_calls = lambda: len([c for c in w.boxen_calls(skip_ls=False) if c["args"] == ["ls", "--json"]])  # noqa: E731
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        n0 = ls_calls()
        await pilot.pause(3.2)
        assert ls_calls() >= n0 + 2                      # attached and focused: every second
        w.write("detached", "")                          # the client detaches
        await wait_for(pilot, lambda: not app.dashboard.tmux.attached, what="detach seen")
        await pilot.pause(1.2)
        n1, s1 = ls_calls(), sum(w.server.counts.values())
        await pilot.pause(3.2)
        assert ls_calls() == n1                          # detached: the idle rate
        assert sum(w.server.counts.values()) == s1       # and no stats
        os.unlink(os.path.join(w.fake, "detached"))
        await wait_for(pilot, lambda: app.dashboard.tmux.attached, what="reattach seen")
        app.app_focus = False                            # attached but the terminal isn't focused
        await pilot.pause(1.2)
        n2 = ls_calls()
        await pilot.pause(2.5)
        assert ls_calls() == n2
        app.app_focus = True
        await wait_for(pilot, lambda: ls_calls() > n2, what="polling again")


@aio
async def test_docker_state_doesnt_flap(world):
    """The reviewer's flap harness: once Docker is gone it stays down."""
    w = world()
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await wait_for(pilot, lambda: app.dashboard.docker_ok is True and app.dashboard.docker_root, what="docker up")
        w.close()
        seen = []
        for _ in range(40):
            await pilot.pause(0.25)
            seen.append("U" if app.dashboard.docker_ok else "D")
        s = "".join(seen)
        assert "D" in s and "DU" not in s, s


def test_job_cap_and_dropped_lines(monkeypatch):
    import boxen_ui.jobs as jobsmod

    class L:
        def job_changed(self, j): pass
        def job_line(self, j, line): pass
        def job_done(self, j): pass
    jm = jobsmod.JobManager("boxen", L(), env={})
    for i in range(60):
        j = jm.start(["up", "p%d" % i], project="p%d" % i)
        jm._finish(j, 0)
    assert len(jm.jobs) == jobsmod.MAX_FINISHED + 1 and jm.jobs[0].id == 10
    monkeypatch.setattr(jobsmod, "MAX_LINES", 10)
    j = jm.start(["up", "x"], project="x")
    for i in range(25):
        jm._line(j, "line %d" % i)
    assert len(j.lines) == 10 and j.dropped == 15 and j.lines[0] == "line 15"


@aio
async def test_unsupported_docker_endpoint(world):
    w = world()
    w.env["BOXEN_DOCKER_HOST"] = "tcp://10.0.0.5:2376"
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await wait_for(pilot, lambda: "docker unsupported endpoint (BOXEN_DOCKER_HOST)" in screen_text(app),
                       what="header says so")
        assert sum(w.server.counts.values()) == 0          # nothing else was asked


def test_docker_endpoint_rules():
    from boxen_ui.config import docker_endpoint
    assert docker_endpoint({"BOXEN_DOCKER_HOST": "unix:///run/d.sock"}) == ("/run/d.sock", None)
    assert docker_endpoint({"BOXEN_DOCKER_HOST": "ssh://vm"})[0] is None
    # your rootless daemon, as bin/boxen finds it; DOCKER_HOST and contexts don't count
    assert docker_endpoint({"XDG_RUNTIME_DIR": "/run/user/1000", "DOCKER_HOST": "unix:///var/run/docker.sock",
                            "DOCKER_CONTEXT": "x"}) == ("/run/user/1000/docker.sock", None)
    assert docker_endpoint({}) == ("/run/user/%d/docker.sock" % os.getuid(), None)


@aio
async def test_ls_failure_keeps_header_and_marks_stale(world):
    w = world()
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await wait_for(pilot, lambda: app.dashboard.sys.get("load"), what="sys line")
        w.write("ls.fail", "")
        app.dashboard.refresh_ls()
        await wait_for(pilot, lambda: app.dashboard.ls_error, what="ls fails")
        await pilot.pause(0.2)
        hdr = str(app.dashboard.query_one("#header").render())
        lines = hdr.split("\n")
        assert len(lines) == 3 and lines[1].startswith("load ") and lines[2].startswith("boxen ls failed: ")
        assert "PROJECT (stale)" in screen_text(app)
        assert rows(app, "#projects")[0] == "caulker"          # the last known list stays


@aio
async def test_zero_projects_getting_started(world):
    w = world(ls={"projects": []})
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await wait_for(pilot, lambda: app.dashboard.ls_loaded, what="ls")
        await pilot.pause(0.3)
        t = screen_text(app)
        assert "No projects yet" in t and "Move from boxen v1" in t and "README.md" in t
        for gone in ("cpu", "memory", "Sidecars", "GitHub repos"):
            assert gone not in t, gone


@aio
async def test_ctrl_q_asks(world):
    w = world()
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("ctrl+q")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="quit asks")
        await pilot.press("n")
        await pilot.pause(0.2)
        assert app.is_running and w.tmux_calls("kill-session") == []


@aio
async def test_update_banner(world, tmp_path):
    import subprocess as sp
    repo = tmp_path / "repo"
    sp.run(["git", "init", "-q", str(repo)], check=True)
    g = ["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "x"]
    sp.run(g, check=True)
    w = world()
    w.env["BOXEN_REPO"] = str(repo)
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await wait_for(pilot, lambda: app.dashboard.git_start, what="git hash")
        assert not app.dashboard.query_one("#banner").has_class("shown")
        sp.run(g, check=True)
        app.dashboard._refresh_git()
        await wait_for(pilot, lambda: app.dashboard.query_one("#banner").has_class("shown"), what="banner")
        assert "boxen ui --restart" in str(app.dashboard.query_one("#banner").render())
        titles = [c.title for c in app.get_system_commands(app.dashboard)]
        assert "Restart dashboard" in titles


@aio
async def test_injection_harness(world):
    """The reviewer's harness: nothing from outside reaches the terminal
    as a control sequence, wherever it's shown."""
    import io
    import re as _re

    from rich.console import Console
    P = ("<\x1b]52;c;UEFZ\x07|\x1b]8;;http://ev\x1b\\L\x1b]8;;\x1b\\|\x1b[2J|\u009b2J|\x1bP+q\x1b\\|\x1b]0;TT\x07|"
         "\x07|AAA\rBBB|‮RTL|x y|\x1b_apc\x1b\\|\x1bc|\x1b[?1049h|\u0085|[b]m[/b]>")
    BAD = [b"]52", b"]8;", b"]0;", b"\x1bP", b"\x1b_", b"\x1bc", b"?1049h", b"\xc2\x9b", b"\xc2\x85", b"\x07",
           b"\xe2\x80\xae"]

    def dump(app):
        upd = app.screen._compositor.render_full_update()
        buf = io.StringIO()
        Console(file=buf, force_terminal=True, color_system="truecolor", width=160, legacy_windows=False).print(
            upd, end="")
        raw = buf.getvalue().encode("utf-8", "surrogateescape")
        return _re.sub(rb"\x1b\[[0-9;:]*[mH]", b"", raw)

    found = {}

    def check(app, where):
        raw = dump(app)
        hits = [b.decode("latin1") for b in BAD if b in raw]
        if _re.search(rb"\x1b", raw):
            hits.append("ESC")
        if hits:
            found[where] = hits

    doc = four_projects()
    c = doc["projects"][0]
    c["container"]["status"] = "Up " + P
    c["repo"] = "r" + P
    c["repos"] = [{"repo": "r" + P, "state": P, "origin": True}]
    c["sidecars"][0]["status"] = P
    c["sidecars"].append(sidecar("caulker", "x" + P))
    c["github"] = P
    doc["v1"] = ["old" + P]
    w = world(ls=doc)
    w.write("capture.txt", "cap " + P + "\n\x1b[31mred\x1b[0m\n")
    w.windows([{"id": "@0", "name": "dashboard"}, {"id": "@3", "name": "caulker/shell" + P},
               {"id": "@4", "name": "caulker"}])
    w.rules([{"args": ["env", "logs", "postgres", "--tail", "300"], "out": ["log " + P]},
             {"args": ["repos", "caulker", "--suggest"], "out": ["found " + P]},
             {"args": ["up", "caulker"], "out": ["fail " + P], "rc": 1}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=(160, 45)) as pilot:
        await started(pilot, app)
        await wait_for(pilot, lambda: app.dashboard.suggest.get("caulker"), what="found repos")
        await pilot.pause(0.5)
        check(app, "overview")
        await pilot.press("G")
        await wait_for(pilot, lambda: isinstance(app.screen, ReposScreen), what="repos dialog")
        await pilot.pause(0.2)
        check(app, "repos-dialog")
        await pilot.press("escape")
        await pilot.pause(0.2)
        app.dashboard.action_sessions_tab()
        await wait_for(pilot, lambda: rows(app, "#win-list"), what="windows")
        await pilot.pause(1.5)
        check(app, "sessions")
        await pilot.press("l")
        await pilot.pause(0.6)
        check(app, "logs")
        app.dashboard.action_files_tab()
        await pilot.pause(0.3)
        check(app, "files")
        await pilot.press("J", "u")
        await job_done(pilot, app)
        await pilot.pause(0.3)
        check(app, "job-failed+notify")
    assert not found, found


def test_long_lines_split_between_characters():
    from boxen_ui.jobs import SPLIT, LineSplitter
    out = []
    sp = LineSplitter(out.append)
    head = "é" * ((SPLIT + 1) // 2)                 # 2-byte characters across the cut
    tail = "the end of a long line"
    data = ("short\n" + head + tail + "\n" + tail + "\n").encode()
    for i in range(0, len(data), 4096):
        sp.feed(data[i:i + 4096])
    sp.close()
    assert out[0] == "short"
    pieces = out[1:-1]
    assert len(pieces) >= 2 and all(len(p.encode()) <= SPLIT for p in pieces)
    assert "�" not in "".join(pieces) and "".join(pieces) == head + tail
    assert out[-1] == tail


# ------------------------------------------------------------ GitHub repos

def repos_doc(extra=()):
    doc = four_projects()
    doc["projects"][0]["github"] = "token partial"
    doc["projects"][0]["repos"] = [{"repo": "theycallmeboxy/caulker", "state": "ok", "origin": True},
                                   {"repo": "upstream/lib", "state": "not-installed", "origin": False},
                                   {"repo": "theycallmeboxy/private", "state": "no-access", "origin": False}] + list(extra)
    return doc


def repo_cells(app):
    t = app.dashboard.query_one("#repos")
    return [[t.get_cell_at((r, c)).plain for c in range(3)] for r in range(t.row_count)]


@aio
async def test_repos_table_and_found_add_asks(world):
    w = world(ls=repos_doc())
    w.rules([{"args": ["repos", "caulker", "--suggest"], "out": ["you/fork"]}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await wait_for(pilot, lambda: len(repo_cells(app)) == 4, what="repos table with the found row")
        assert repo_cells(app) == [["theycallmeboxy/caulker", "ok", "origin"],
                                   ["upstream/lib", "not your repo", ""],
                                   ["theycallmeboxy/private", "no access", ""],
                                   ["you/fork", "found, not listed", "a adds"]]
        assert "token partial" in screen_text(app) and "partial" in screen_text(app)
        await pilot.press("a")                 # the only found one: asks first
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="confirm")
        assert "Let caulker push to you/fork?" in screen_text(app)
        assert app.focused is app.screen.query_one("#no")
        await pilot.press("n")
        await pilot.pause(0.2)
        assert w.boxen_calls() == []
        await pilot.press("a")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="confirm")
        await pilot.press("y")
        await job_done(pilot, app)
        assert [c["args"] for c in w.boxen_calls()] == [["repos", "caulker", "add", "you/fork"]]


@aio
async def test_repos_dialog_add_validates_and_remove(world):
    w = world(ls=repos_doc())
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("G")
        await wait_for(pilot, lambda: isinstance(app.screen, ReposScreen), what="repos dialog")
        inp = app.screen.query_one("#repo-input")
        for bad in ("you", "a/b/c", "git@github.com:a/b", "https://gitlab.com/a/b", "a/../b", "[bold]x/y"):
            inp.value = bad
            await press_button(pilot, app, "#add")
            assert isinstance(app.screen, ReposScreen), bad
            assert "isn't a GitHub repo" in str(app.screen.query_one("#error").render())
        inp.value = "you/fork, https://github.com/myorg/x.git YOU/fork"
        await press_button(pilot, app, "#add")
        await job_done(pilot, app)
        assert w.boxen_calls()[-1]["args"] == ["repos", "caulker", "add", "you/fork", "myorg/x"]
        # remove: select a listed one, x, confirm
        await pilot.press("G")
        await wait_for(pilot, lambda: isinstance(app.screen, ReposScreen), what="repos dialog")
        t = app.screen.query_one("#repo-list")
        t.focus()
        t.move_cursor(row=1)
        await pilot.pause(0.1)
        await pilot.press("x")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="confirm")
        await pilot.press("y")
        await job_done(pilot, app, 2)
        assert w.boxen_calls()[-1]["args"] == ["repos", "caulker", "rm", "upstream/lib"]


@aio
async def test_repos_injection_renders_literally(world):
    evil = [INJECT_LINK, INJECT_CLICK, "\x1b]8;;http://x\x1b\\a\x1b]8;;\x1b\\/b", "a/b\x1b[2J"]
    w = world(ls=repos_doc(extra=[{"repo": INJECT_CLICK, "state": INJECT_LINK, "origin": True}]))
    w.rules([{"args": ["repos", "caulker", "--suggest"], "out": evil}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=(160, 45)) as pilot:
        await started(pilot, app)
        await wait_for(pilot, lambda: len(repo_cells(app)) == 8, what="repos table")
        cells = repo_cells(app)
        assert cells[3][0] == INJECT_CLICK and cells[4][0] == INJECT_LINK and cells[5][0] == INJECT_CLICK
        assert cells[6][0] == "a/b" and cells[7][0] == "a/b"            # escapes stripped
        assert cells[4][2] == "not a repo name"
        txt = screen_text(app)
        assert INJECT_LINK in txt
        # a on an invalid found row adds nothing; the valid ones need a pick
        t = app.dashboard.query_one("#repos")
        t.focus()
        t.move_cursor(row=4)
        await pilot.press("a")
        await pilot.pause(0.3)
        assert w.boxen_calls() == [] and not isinstance(app.screen, ConfirmScreen)
        await pilot.press("G")
        await wait_for(pilot, lambda: isinstance(app.screen, ReposScreen), what="repos dialog")
        assert INJECT_LINK in screen_text(app)
        assert app.is_running


# ------------------------------------------------------------ serve, quitting, restarting

@aio
async def test_served_sessions_shown_and_warned_about(world):
    doc = four_projects()
    doc["projects"][0]["served"] = 3                # the server and two sessions started from the app
    doc["projects"][1]["served"] = 1                # beta: only a server (serve off, window still up)
    w = world(ls=doc)
    w.windows([{"id": "@0", "name": "dashboard"}, {"id": "@4", "name": "caulker~serve", "sessions": ["serve"]}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        t = app.dashboard.query_one("#projects")
        assert [t.get_cell(k, "sess").plain for k in ("caulker", "beta", "gamma")] == ["2+2", "-", "-"]
        assert "2 Claude processes in the folder + 2 from the app, via serve" in \
            str(app.dashboard.query_one("#ov-sessions").render())
        await pilot.press("S")                      # serve on: asks to turn it off
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="serve off asks")
        assert "Sessions started from the Claude app through it stop with it (2 running now)" in \
            str(app.screen._message)
        await pilot.press("n")
        app.dashboard.action_sessions_tab()
        await wait_for(pilot, lambda: rows(app, "#win-list") == ["@4"], what="serve window")
        app.dashboard.query_one("#win-list").focus()
        await pilot.press("x")                      # a serve window: x hides it, never asks to end it
        await pilot.pause(0.3)
        assert not isinstance(app.screen, ConfirmScreen) and w.tmux_calls("kill-window") == []
        app.dashboard.query_one("#projects").focus()
        await pilot.press("down")
        await wait_for(pilot, lambda: app.dashboard.selected == "beta", what="beta")
        assert "none running" in str(app.dashboard.query_one("#ov-sessions").render())


@aio
async def test_claude_closes_finished_sessions_first(world):
    w = world()
    w.windows([{"id": "@0", "name": "dashboard"}, {"id": "@3", "name": "caulker", "done": True},
               {"id": "@4", "name": "caulker", "done": True}, {"id": "@5", "name": "caulker/shell", "done": True}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("c")
        await wait_for(pilot, lambda: w.tmux_calls("new-window"), what="claude window")
        assert [c[-1] for c in w.tmux_calls("kill-window")] == ["@3", "@4"]       # not the shell
        await pilot.press("C")
        await wait_for(pilot, lambda: len(w.tmux_calls("new-window")) == 2, what="second session")
        assert len(w.tmux_calls("kill-window")) == 2                              # the new one still runs


@aio
async def test_quit_and_restart_name_the_running_jobs(world):
    w = world()
    w.rules([{"args": ["up", "caulker"], "out": ["working"], "sleep": 30},
             {"args": ["ui", "--restart"], "out": ["boxen: the ui's Python environment is missing"], "rc": 1}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("u")
        await wait_for(pilot, lambda: app.dashboard.jobs.jobs and app.dashboard.jobs.jobs[0].lines, what="job")
        pid = w.boxen_calls()[0]["pid"]
        await pilot.press("Q")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="quit asks")
        assert "These running jobs will be stopped (SIGTERM, then SIGKILL after 5 s" in str(app.screen._message)
        assert "up caulker" in str(app.screen._message)
        await pilot.press("n")
        app.dashboard.action_restart_dashboard()
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="restart asks")
        assert "up caulker" in str(app.screen._message)
        await pilot.press("y")
        # the job is stopped first (it ends "stopped"), then boxen ui --restart runs; its failure is shown
        want = "Couldn't restart the dashboard: boxen: the ui's Python environment is missing"
        await wait_for(pilot, lambda: want in [n.message for n in app._notifications], what="restart failure shown",
                       timeout=12)
        gone(pid)
        assert app.dashboard.jobs.jobs[0].status == "stopped"
        assert [c["args"] for c in w.boxen_calls()] == [["up", "caulker"], ["ui", "--restart"]]
        assert app.is_running


@aio
async def test_job_gets_a_grace_period(world, tmp_path):
    """Quitting sends SIGTERM once and waits: a job that cleans up on TERM
    (as boxen new does) finishes its cleanup before anything is killed."""
    mark = tmp_path / "cleaned"
    slow = tmp_path / "slow-boxen"
    slow.write_text("#!/bin/sh\n[ \"$1\" = up ] || exit 1\n"
                    "trap 'sleep 1; touch %s; exit 143' TERM\necho working\nwhile :; do sleep 0.1; done\n" % mark)
    slow.chmod(0o755)
    w = world()
    w.env["BOXEN_UI_BOXEN"] = str(slow)
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await wait_for(pilot, lambda: app.dashboard.ls_loaded, what="ls")
        app.dashboard.start_job(["up", "caulker"], project="caulker")
        await wait_for(pilot, lambda: app.dashboard.jobs.jobs[0].lines, what="job")
        await pilot.press("Q")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="quit asks")
        await pilot.press("y")
        await wait_for(pilot, lambda: not app.is_running, what="exit", timeout=10)
    assert mark.exists()


# ---------------------------------------------------------------- disk use

def df_doc(docker=True):
    """`boxen df --json` for caulker (built, a sidecar) and beta (no image)."""
    return {"version": 1, "docker": docker, "projects": [
        {"name": "caulker", "path": "/x/caulker", "total": 3 * 2**30, "code": 2**30, "claude": 4 * 2**20,
         "claude_path": "/x/c", "image": {"name": "boxen-env-caulker:latest", "size": 500 * 2**20},
         "containers": [{"name": "boxen.caulker.env", "size": 12 * 2**20},
                        {"name": "boxen.caulker.postgres", "size": 2**20}],
         "volumes": [{"name": "boxen.caulker.cache", "size": 300 * 2**20},
                     {"name": "boxen.caulker.pgdata", "size": 40 * 2**20}]},
        {"name": "beta", "path": "/x/beta", "total": 5 * 2**20, "code": 5 * 2**20, "claude": None,
         "claude_path": "/x/b", "image": None, "containers": [], "volumes": []}],
        "shared": {}, "other": {}, "leftovers": [], "filesystems": [], "total": 0}


def ov_disk(app):
    return str(app.dashboard.query_one("#ov-disk").render())


@aio
async def test_disk_column_and_overview(world, monkeypatch):
    import boxen_ui.app as appmod
    monkeypatch.setattr(appmod, "DF_FIRST", 60)          # not measured yet at first
    w = world()
    w.write("df.json", json.dumps(df_doc()))
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        t = app.dashboard.query_one("#projects")
        assert [c.value for c in t.columns] == ["name", "ctr", "sess", "serve", "cpu", "mem", "disk", "gh"]
        assert t.get_cell("caulker", "disk").plain == "-"
        assert ov_disk(app) == "measuring…"
        assert not [c for c in w.boxen_calls(skip_ls=False) if c["args"][:1] == ["df"]]
        app.dashboard.refresh_df()
        await wait_for(pilot, lambda: app.dashboard.disk is not None, what="df")
        await pilot.pause(0.1)
        assert [t.get_cell(k, "disk").plain for k in ("caulker", "beta", "gamma")] == ["3.0G", "5.0M", "-"]
        line = ov_disk(app)
        assert line.startswith("3.0G  code 1.0G · image 500M · changes 13M · volumes 340M · claude 4.0M")
        assert "(just now)" in line
        await pilot.press("down")
        assert ov_disk(app).startswith("5.0M  code 5.0M · image - · changes 0B · volumes 0B · claude ?")
        await pilot.press("down")
        assert ov_disk(app) == "not measured yet"          # gamma isn't in the report


@aio
async def test_disk_without_docker_shows_unknowns(world):
    w = world()
    doc = df_doc(docker=False)
    doc["projects"][0]["image"] = None
    w.write("df.json", json.dumps(doc))
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await wait_for(pilot, lambda: app.dashboard.disk is not None, what="df")
        await pilot.pause(0.1)
        line = ov_disk(app)
        assert "image ? · changes ? · volumes ?" in line and "Docker wasn't answering" in line


@aio
async def test_disk_refresh_cadence(world, monkeypatch):
    import boxen_ui.app as appmod
    monkeypatch.setattr(appmod, "DF_FIRST", 0)
    monkeypatch.setattr(appmod, "DF_EVERY", 1)
    w = world()
    # each measurement takes 1.5 s: longer than DF_EVERY
    w.rules([{"args": ["df", "--json"], "out": [json.dumps(df_doc())], "sleep": 1.5}])
    calls = lambda: len([c for c in w.boxen_calls(skip_ls=False) if c["args"] == ["df", "--json"]])  # noqa: E731
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await wait_for(pilot, lambda: calls() == 1, what="the first df")
        await pilot.pause(1.2)
        assert calls() == 1                                # never two at once
        await wait_for(pilot, lambda: app.dashboard.disk is not None, what="the first result")
        await wait_for(pilot, lambda: calls() == 2, timeout=4, what="the next one")
        w.write("detached", "")                            # nobody attached: no more
        await wait_for(pilot, lambda: not app.dashboard.tmux.attached, what="detach seen")
        await wait_for(pilot, lambda: "df" not in app.dashboard._busy, what="the running one to finish")
        n = calls()
        await pilot.pause(2.5)
        assert calls() == n
        # ctrl+r: only when the last one is older than DF_MIN_AGE
        os.unlink(os.path.join(w.fake, "detached"))
        monkeypatch.setattr(appmod, "DF_EVERY", 600)
        await wait_for(pilot, lambda: app.dashboard.tmux.attached, what="reattach seen")
        await wait_for(pilot, lambda: "df" not in app.dashboard._busy, what="idle")
        n = calls()
        await pilot.press("ctrl+r")
        await pilot.pause(0.5)
        assert calls() == n
        monkeypatch.setattr(appmod, "DF_MIN_AGE", 0)
        await pilot.press("ctrl+r")
        await wait_for(pilot, lambda: calls() == n + 1, what="ctrl+r measuring again")


@aio
async def test_disk_failure_keeps_old_data(world, monkeypatch):
    import boxen_ui.app as appmod
    monkeypatch.setattr(appmod, "DF_MIN_AGE", 0)
    w = world()
    w.write("df.json", json.dumps(df_doc()))
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await wait_for(pilot, lambda: app.dashboard.disk is not None, what="df")
        old = app.dashboard.disk
        w.write("df.fail", "")
        await wait_for(pilot, lambda: "df" not in app.dashboard._busy, what="idle")
        await pilot.press("ctrl+r")
        await wait_for(pilot, lambda: app.dashboard.disk_error, what="the failure")
        await pilot.pause(0.1)
        assert app.dashboard.disk is old
        cell = app.dashboard.query_one("#projects").get_cell("caulker", "disk")
        assert cell.plain == "3.0G" and "dim" in str(cell.style)
        assert "last try failed: boxen: something broke in df" in ov_disk(app)
        # garbage instead of the report: the same
        os.unlink(os.path.join(w.fake, "df.fail"))
        w.rules([{"args": ["df", "--json"], "out": ["not json " + INJECT_LINK]}])
        app.dashboard.disk_error = None
        await pilot.press("ctrl+r")
        await wait_for(pilot, lambda: app.dashboard.disk_error, what="the parse failure")
        assert app.dashboard.disk is old
        # recovers
        w.rules([])
        await wait_for(pilot, lambda: "df" not in app.dashboard._busy, what="idle")
        await pilot.press("ctrl+r")
        await wait_for(pilot, lambda: app.dashboard.disk_error is None, what="recovery")
        assert "failed" not in ov_disk(app)


@aio
async def test_disk_first_failure_says_so(world):
    w = world()
    w.write("df.fail", "")
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await wait_for(pilot, lambda: app.dashboard.disk_error, what="the failure")
        await pilot.pause(0.1)
        assert ov_disk(app) == "boxen df failed: boxen: something broke in df"
        assert app.dashboard.query_one("#projects").get_cell("caulker", "disk").plain == "-"


@aio
async def test_disk_usage_is_in_the_global_menu(world):
    from boxen_ui.screens import GLOBAL_ACTIONS
    w = world()
    w.rules([{"args": ["df"], "out": ["PROJECT  TOTAL", "Shared by the projects", "  build cache  1.8G"]}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        assert "disk" in [k for k, _t, _d in GLOBAL_ACTIONS]
        await pilot.press("g")
        await wait_for(pilot, lambda: isinstance(app.screen, GlobalMenuScreen), what="the menu")
        ol = app.screen.query_one("#global-options")
        ol.highlighted = [k for k, _t, _d in GLOBAL_ACTIONS].index("disk")
        await pilot.press("enter")
        await job_done(pilot, app)
        assert [c["args"] for c in w.boxen_calls()] == [["df"]]
        assert not app.dashboard.jobs.jobs[0].is_global          # read-only: holds nothing up
        assert "build cache  1.8G" in screen_text(app)


# ------------------------------------------------------- serve windows: hide/show

def binding_for(app, key):
    b = app.screen.active_bindings.get(key)
    return b.binding.action if b else None


@aio
async def test_hidden_serve_window_is_listed_and_shown(world):
    w = world()
    w.windows([{"id": "@0", "name": "dashboard"}, {"id": "@3", "name": "caulker", "sessions": ["boxen"]},
               {"id": "@4", "name": "caulker~serve", "sessions": ["serve"]},
               {"id": "@5", "name": "beta~serve", "sessions": ["serve"]}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await wait_for(pilot, lambda: "hidden: Sessions tab, Enter shows it" in
                       str(app.dashboard.query_one("#ov-serve").render()), what="overview says hidden")
        assert ["-L", "boxen", "list-windows", "-t", "=serve:"] in [c[:5] for c in w.tmux_calls("list-windows")]
        assert all(c[4] in ("=boxen:", "=serve:") for c in w.tmux_calls("list-windows"))
        app.dashboard.action_sessions_tab()
        await wait_for(pilot, lambda: rows(app, "#win-list") == ["@3", "@4"], what="both windows, not beta's")
        t = app.dashboard.query_one("#win-list")
        assert t.get_cell("@4", "state").plain == "running, hidden"
        assert t.get_cell("@3", "state").plain == "running"
        t.focus()
        assert binding_for(app, "x") == "window_close"           # a Claude session: Close
        await pilot.press("down")
        await wait_for(pilot, lambda: binding_for(app, "x") == "window_hide", what="x is Hide")
        assert "Hide" in screen_text(app)
        # Enter shows it: linked into your session, then selected
        await pilot.press("enter")
        await wait_for(pilot, lambda: w.tmux_calls("link-window"), what="link")
        assert w.tmux_calls("link-window")[0] == ["-L", "boxen", "link-window", "-d", "-s", "=serve:@4",
                                                   "-t", "=boxen:"]
        await wait_for(pilot, lambda: ["-t", "=boxen:@4"] == w.tmux_calls("select-window")[-1][-2:],
                       what="select")
        await wait_for(pilot, lambda: t.get_cell("@4", "state").plain == "running, shown", what="shown")
        # x hides it again: unlinked from your session, never killed, no question asked
        await pilot.press("x")
        await wait_for(pilot, lambda: w.tmux_calls("unlink-window"), what="unlink")
        assert w.tmux_calls("unlink-window")[0] == ["-L", "boxen", "unlink-window", "-t", "=boxen:@4"]
        await wait_for(pilot, lambda: t.get_cell("@4", "state").plain == "running, hidden", what="hidden")
        assert w.tmux_calls("kill-window") == [] and not isinstance(app.screen, ConfirmScreen)
        assert any(x["name"] == "caulker~serve" and x["sessions"] == ["serve"]
                   for x in json.load(open(os.path.join(w.fake, "windows.json"))))
        # x on a hidden one: nothing to do
        n = len(w.tmux_calls())
        await pilot.press("x")
        await pilot.pause(0.3)
        assert [c for c in w.tmux_calls()[n:] if "unlink-window" in c or "kill-window" in c] == []
        # a Claude session window: x still asks to close it
        await pilot.press("up")
        await wait_for(pilot, lambda: binding_for(app, "x") == "window_close", what="x is Close")
        await pilot.press("x")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="close asks")


@aio
async def test_hiding_a_serve_window_only_in_your_session_is_refused(world):
    """An older serve window (in `boxen` only): tmux won't unlink it, so it
    keeps running, and the dashboard says how to move it."""
    w = world()
    w.windows([{"id": "@0", "name": "dashboard"}, {"id": "@4", "name": "caulker~serve"}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        app.dashboard.action_sessions_tab()
        await wait_for(pilot, lambda: rows(app, "#win-list") == ["@4"], what="serve window")
        app.dashboard.query_one("#win-list").focus()
        await pilot.press("x")
        await wait_for(pilot, lambda: w.tmux_calls("unlink-window"), what="unlink tried")
        await wait_for(pilot, lambda: any("boxen ui --restart moves serve windows" in n.message
                                          for n in app._notifications), what="the notice")
        assert w.tmux_calls("kill-window") == []
        assert [x["id"] for x in json.load(open(os.path.join(w.fake, "windows.json")))] == ["@0", "@4"]


def test_tmux_conf_hide_key(tmp_path):
    """C-b h and the status hint, in a real tmux (skipped without one)."""
    import shutil
    import subprocess
    if not shutil.which("tmux"):
        pytest.skip("no tmux")
    conf = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmux.conf")
    sock = "bxt%d" % os.getpid()
    t = ["tmux", "-L", sock]

    def tm(*a):
        return subprocess.run(t + list(a), capture_output=True, text=True, timeout=10)
    try:
        assert tm("-f", conf, "new-session", "-d", "-s", "boxen", "-n", "dashboard", "sleep", "60").returncode == 0
        assert tm("new-session", "-d", "-s", "serve", "-n", "caulker~serve", "sleep", "60").returncode == 0
        keys = tm("list-keys", "-T", "prefix", "h").stdout
        assert "unlink-window -t =boxen:" in keys and "select-window -t =boxen:=dashboard" in keys
        right = tm("show", "-gv", "status-right").stdout.strip()
        tm("link-window", "-d", "-s", "=serve:=caulker~serve", "-t", "=boxen:")
        tm("select-window", "-t", "=boxen:=caulker~serve")
        assert tm("display-message", "-p", "-t", "=boxen:", right).stdout.startswith("#[fg=colour244]C-b h hide")
        tm("select-window", "-t", "=boxen:=dashboard")
        assert "C-b h" not in tm("display-message", "-p", "-t", "=boxen:", right).stdout
        # what the key runs, with its target: the serve window leaves your tabs and keeps running
        tm("select-window", "-t", "=boxen:=caulker~serve")
        cmd = keys.split(" h ", 1)[1].strip()
        assert tm("if-shell", "-t", "=boxen:", "-F", "#{m:*~serve,#{window_name}}",
                  "unlink-window -t =boxen: ; select-window -t =boxen:=dashboard").returncode == 0, cmd
        names = lambda s: tm("list-windows", "-t", "=%s:" % s, "-F", "#{window_name}").stdout.split()  # noqa: E731
        assert names("boxen") == ["dashboard"] and names("serve") == ["caulker~serve"]
    finally:
        tm("kill-server")
