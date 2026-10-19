"""Headless tests of boxen ui (Textual Pilot) against fake boxen, docker,
tmux and Docker Engine API. Run: python -m pytest ui/tests"""
import json
import os

import pytest
from conftest import aio, four_projects, project, screen_text, wait_for

from boxen_ui import procs
from boxen_ui.app import BoxenApp, editor_argv
from boxen_ui.jobs import parse_approval, question_project
from boxen_ui.screens import (ApprovalScreen, ConfirmScreen, GlobalMenuScreen, NewProjectScreen, QuestionScreen,
                              wrap_question)
from boxen_ui.tmux import hold_script

SIZE = (120, 35)
INJECT_LINK = "[link=file:///etc/passwd]x[/link]"
INJECT_CLICK = "[@click=app.quit]boom[/]"

TRUST_BLOCK = [
    "boxen: approval needed (trust) for caulker:",
    "    Remote Control starts Claude Code sessions in this folder.",
    "    " + INJECT_LINK + " MCP servers: [bold]evil[/bold]",
    "    Trust /home/boxy/src/caulker? [y/N]",
    "boxen: to approve, run: boxen up caulker --approve trust",
]


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


@aio
async def test_list_renders(world):
    w = world()
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        assert rows(app, "#projects") == ["caulker", "beta", "gamma", "--sep--", "supervisor"]
        want = ("caulker", "running", "connected", "exited", "stopped", "waiting", "stale", "supervisor")
        await wait_for(pilot, lambda: all(x in screen_text(app) for x in want), what="the list on screen")
        # docker stats arrive (fake API): CPU/memory for running containers
        await wait_for(pilot, lambda: app.dashboard.stats.get("boxen-caulker", {}).get("cpu") is not None,
                       what="cpu stats")
        assert app.dashboard.stats["boxen-caulker"]["mem"] > 0
        assert "docker up" in screen_text(app)


@aio
async def test_selection_updates_tabs(world):
    w = world()
    w.write("carry-gamma.tsv", "1\tabcdef12\t2026-10-01\t5\t1\t-\tFix the parser\n")
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        assert app.dashboard.selected == "caulker"
        await pilot.press("down")
        assert app.dashboard.selected == "beta"
        await pilot.press("j")
        assert app.dashboard.selected == "gamma"
        txt = screen_text(app)
        assert "waiting for an answer: run boxen up gamma" in txt
        # the separator row is skipped
        await pilot.press("down")
        assert app.dashboard.selected == "supervisor"
        await pilot.press("k")
        assert app.dashboard.selected == "gamma"
        app.dashboard.action_sessions_tab()
        await wait_for(pilot, lambda: rows(app, "#sessions") == ["abcdef12"], what="sessions")
        assert ["exec", "boxen-gamma", "boxen-carry", "list"] in w.docker_calls()


@aio
async def test_up_runs_job_in_agent_mode(world):
    w = world()
    w.rules([{"args": ["up", "caulker"], "out": ["caulker: connected"], "delay": 0.05}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("u")
        await job_done(pilot, app)
        calls = w.boxen_calls()
        assert calls[0]["args"] == ["up", "caulker"]
        assert calls[0]["agent"] == "1"
        job = app.dashboard.jobs.jobs[0]
        assert job.status == "ok" and job.lines == ["caulker: connected"]
        assert rows(app, "#job-table") == ["1"]
        await pilot.press("J")
        assert "caulker: connected" in screen_text(app)


@aio
async def test_streaming_output_and_failure(world):
    w = world()
    w.rules([{"args": ["restart", "caulker"], "out": ["one", "two", "three", "boxen: starting caulker failed:"],
              "delay": 0.3, "rc": 1}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("J", "r")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="restart asks")
        await pilot.press("y")
        await wait_for(pilot, lambda: app.dashboard.jobs.jobs and app.dashboard.jobs.jobs[0].lines[:1] == ["one"],
                       what="first line")
        assert app.dashboard.jobs.jobs[0].status == "running"
        assert "one" in screen_text(app)
        await job_done(pilot, app)
        assert app.dashboard.jobs.jobs[0].status == "failed"
        assert "failed" in screen_text(app)


@aio
async def test_approval_yes_runs_exactly_the_printed_command(world):
    w = world()
    w.rules([{"args": ["up", "caulker"], "out": ["caulker: sidecars: postgres"] + TRUST_BLOCK, "rc": 3}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("u")
        await wait_for(pilot, lambda: isinstance(app.screen, ApprovalScreen), what="approval dialog")
        block = str(app.screen.query_one("#approval-block").render())
        assert block == wrap_question(TRUST_BLOCK[1:-1])        # the question, boxen's words
        assert block.startswith("Remote Control starts") and "approval needed" not in block
        assert "boxen up caulker --approve trust" in str(app.screen.query_one("#approval-cmd").render())
        assert app.focused is app.screen.query_one("#no")
        txt = screen_text(app)
        assert INJECT_LINK in txt and "[bold]evil[/bold]" in txt   # literal, not markup
        await press_button(pilot, app, "#yes")
        await job_done(pilot, app, 2)
        assert [c["args"] for c in w.boxen_calls()] == [["up", "caulker"], ["up", "caulker", "--approve", "trust"]]
        assert all(c["agent"] == "1" for c in w.boxen_calls())


@aio
async def test_approval_no_runs_nothing(world):
    w = world()
    w.rules([{"args": ["up", "caulker"], "out": TRUST_BLOCK, "rc": 3}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("u")
        await wait_for(pilot, lambda: isinstance(app.screen, ApprovalScreen), what="approval dialog")
        await pilot.press("n")
        await pilot.pause(0.5)
        assert not isinstance(app.screen, ApprovalScreen)
        assert [c["args"] for c in w.boxen_calls()] == [["up", "caulker"]]
        assert app.dashboard.jobs.jobs[0].status == "needs approval"


@aio
async def test_rm_approval_then_yes(world):
    w = world()
    w.rules([{"args": ["rm", "caulker"], "rc": 3, "out": [
        "boxen: approval needed (rm) for caulker:",
        "    Remove container, image and config for 'caulker'? Its tool installs (home volume) and sidecar "
        "data are removed too; code in /home/boxy/src/caulker is kept.",
        "boxen: to approve, run: boxen rm caulker --yes"]}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("X")
        await wait_for(pilot, lambda: isinstance(app.screen, ApprovalScreen), what="approval dialog")
        assert "boxen rm caulker --yes" in str(app.screen.query_one("#approval-cmd").render())
        await press_button(pilot, app, "#yes")
        await job_done(pilot, app, 2)
        assert [c["args"] for c in w.boxen_calls()] == [["rm", "caulker"], ["rm", "caulker", "--yes"]]


GOOD = ["noise", "boxen: approval needed (trust) for a:", "    q", "boxen: to approve, run: boxen up a --approve trust"]
GOOD_RM = ["boxen: approval needed (rm) for a:", "    Remove a?", "boxen: to approve, run: boxen rm a --yes"]


def test_parse_approval_accepts_what_boxen_prints():
    a = parse_approval(GOOD, "a", "up")
    assert a.argv == ["boxen", "up", "a", "--approve", "trust"] and a.question == ["    q"]
    assert parse_approval(GOOD, "a", "restart") and parse_approval(GOOD, "a", "new")
    assert parse_approval(GOOD + [""], "a", "up")           # a trailing empty line is fine
    assert parse_approval(GOOD_RM, "a", "rm").argv == ["boxen", "rm", "a", "--yes"]


@pytest.mark.parametrize("lines, project, verb", [
    (GOOD + ["more output"], "a", "up"),                                  # run line not last
    (GOOD[:2] + ["    q\x1b[2A"] + GOOD[3:], "a", "up"),                    # ESC in the block
    (GOOD[:2] + ["    q\rboxen: approval needed (rm) for a:"] + GOOD[3:], "a", "up"),   # CR in the block
    (GOOD, "b", "up"),                                                    # another project's question
    (GOOD, "a", "rm"),                                                    # trust asked by an rm job
    (GOOD_RM, "a", "up"),                                                 # rm asked by an up job
    (GOOD[:3] + ["boxen: to approve, run: boxen rm a --yes"], "a", "up"),     # kind trust, rm answer
    (GOOD_RM[:2] + ["boxen: to approve, run: boxen up a --approve trust"], "a", "rm"),
    (GOOD[:3] + ["boxen: to approve, run: boxen restart a --approve trust"], "a", "restart"),  # only up
    (GOOD[:3] + ["boxen: to approve, run: boxen up a --approve trust; rm -rf ~"], "a", "up"),
    (GOOD[:3] + ["boxen: to approve, run: boxen up a --approve trust --yes"], "a", "up"),
    (GOOD[:3] + ["boxen: to approve, run: boxen  up a --approve trust"], "a", "up"),
    (["boxen: approval needed (trust) for a:", "    q", "not indented", "boxen: to approve, run: boxen up a --approve trust"],
     "a", "up"),                                                          # the block has a gap
    (["    boxen: approval needed (trust) for a:", "    boxen: to approve, run: boxen up a --approve trust"], "a", "up"),
    (["boxen: approval needed (sudo) for a:", "    q", "boxen: to approve, run: boxen up a --approve trust"], "a", "up"),
    (["boxen: to approve, run: boxen up a --approve trust"], "a", "up"),  # no header
])
def test_parse_approval_rejects_forgeries(lines, project, verb):
    assert parse_approval(lines, project, verb) is None


def test_question_project_only_the_jobs_own():
    assert question_project(["boxen: a is waiting at an unrecognized question:"], "a") == "a"
    assert question_project(["boxen: other is waiting at an unrecognized question:"], "a") is None
    assert question_project(["supervisor: needs an answer at a question"], "a") is None


@aio
async def test_unrecognized_question_opens_terminal(world):
    w = world()
    w.rules([{"args": ["up", "caulker"], "rc": 3, "out": [
        "boxen: caulker is waiting at an unrecognized question:", "    Pick a color (r/g/b)",
        "boxen: answer it with 'boxen up caulker' from a terminal on the VM"]}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("u")
        await wait_for(pilot, lambda: isinstance(app.screen, QuestionScreen), what="question dialog")
        assert "Pick a color (r/g/b)" in screen_text(app)
        await press_button(pilot, app, "#terminal")
        await wait_for(pilot, lambda: w.tmux_calls("new-window"), what="new-window")
        nw = w.tmux_calls("new-window")[0]
        assert nw[:6] == ["-L", "boxen", "new-window", "-t", "boxen:", "-c"]
        assert nw[nw.index("-n") + 1] == "caulker/up"
        assert nw[nw.index("-c") + 1] == app.cfg.home
        assert nw[-3:-1] == ["sh", "-c"]
        assert ("%s up caulker;" % w.env["BOXEN_UI_BOXEN"]) in nw[-1]


@aio
async def test_shell_claude_follow_windows(world):
    w = world()
    w.windows([{"id": "@0", "name": "dashboard"}, {"id": "@5", "name": "caulker/claude"}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("s")
        await wait_for(pilot, lambda: w.tmux_calls("new-window"), what="shell window")
        nw = w.tmux_calls("new-window")[0]
        assert nw == ["-L", "boxen", "new-window", "-t", "boxen:", "-c", app.cfg.home, "-n", "caulker/shell", "sh", "-c",
                      hold_script([w.env["BOXEN_UI_BOXEN"], "shell", "caulker"])]
        # an existing window is selected, not opened again
        await pilot.press("c")
        await wait_for(pilot, lambda: w.tmux_calls("select-window"), what="select-window")
        assert w.tmux_calls("select-window")[0][-2:] == ["-t", "@5"]
        assert len(w.tmux_calls("new-window")) == 1
        # s again: now the shell window exists too
        await pilot.press("s")
        await wait_for(pilot, lambda: len(w.tmux_calls("select-window")) == 2, what="second select")
        assert w.tmux_calls("select-window")[1][-1] == "@6"
        # f follows the log in a window (Logs tab)
        await pilot.press("l")
        await pilot.pause(0.2)
        await pilot.press("f")
        await wait_for(pilot, lambda: len(w.tmux_calls("new-window")) == 2, what="logs window")
        nw = w.tmux_calls("new-window")[1]
        assert nw[nw.index("-n") + 1] == "caulker/logs"
        assert nw[-1] == hold_script([w.env["BOXEN_UI_BOXEN"], "logs", "caulker", "-f"])


@aio
async def test_new_project_validation(world):
    w = world()
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("n")
        await wait_for(pilot, lambda: isinstance(app.screen, NewProjectScreen), what="new dialog")
        for bad in ("Bad", "a--b", "all", "supervisor", "-x", "x" * 41):
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
    doc["projects"][0]["server"] = INJECT_CLICK
    doc["projects"].append(project("[bold]x[/bold]", status=None, github=INJECT_LINK))
    w.set_ls(doc)
    w.write("carry-caulker.tsv", "1\tabcdef12\t2026-10-01\t5\t1\t-\t%s %s\n" % (INJECT_LINK, INJECT_CLICK))
    w.rules([
        {"args": ["logs", "caulker", "--tail", "300"], "out": ["log: " + INJECT_LINK, "log: " + INJECT_CLICK,
                                                               "\x1b]8;;file:///etc/passwd\x1b\\osc\x1b]8;;\x1b\\ \x1b[31mred\x1b[0m"]},
        {"args": ["up", "caulker"], "out": ["job: " + INJECT_LINK, "job: " + INJECT_CLICK]},
    ])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.pause(0.2)
        txt = screen_text(app)
        assert "[bold]x[/bold]" in txt              # project name in the list, literal
        assert INJECT_CLICK[:24] in txt             # server state (Overview), literal
        app.dashboard.action_sessions_tab()
        await wait_for(pilot, lambda: rows(app, "#sessions") == ["abcdef12"], what="sessions")
        await pilot.pause(0.1)
        txt = screen_text(app)
        title = app.dashboard.query_one("#sessions").get_cell("abcdef12", "title")
        assert title.plain.startswith(INJECT_LINK[:16]) and not title.spans   # literal (maybe cut with …)
        assert INJECT_LINK[:16] in txt
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
async def test_terminal_preview_and_switch(world):
    w = world()
    w.windows([{"id": "@0", "name": "dashboard"}, {"id": "@3", "name": "caulker/shell"},
               {"id": "@4", "name": "other/shell"}])
    w.write("capture.txt", "$ ls\n\x1b[32mgreen.txt\x1b[0m " + INJECT_LINK + "\n"
            "\x1b]8;;file:///etc/passwd\x1b\\click\x1b]8;;\x1b\\\x1b]0;title\x07\n")
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        app.dashboard.query_one("#tabs").active = "terminals"
        await wait_for(pilot, lambda: rows(app, "#term-list") == ["@3"], what="window list")
        await wait_for(pilot, lambda: "green.txt" in screen_text(app), what="preview")
        txt = screen_text(app)
        assert INJECT_LINK in txt and "click" in txt and "title" not in txt
        prev = app.dashboard.query_one("#term-preview").render()
        assert not any(getattr(s.style, "link", None) for s in prev.spans)
        app.dashboard.query_one("#term-list").focus()
        await pilot.press("enter")
        await wait_for(pilot, lambda: w.tmux_calls("select-window"), what="select")
        assert w.tmux_calls("select-window")[0][-1] == "@3"


@aio
async def test_sessions_actions(world):
    w = world()
    w.write("carry-caulker.tsv", "1\tabcdef12\t2026-10-01\t5\t1\tlive\tFirst\n"
                                 "2\t0123abcd\t2026-09-01\t9\t2\t-\tSecond\n"
                                 "3\t--stop\t2026-09-01\t9\t2\t-\tforged id is skipped\n")
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        app.dashboard.action_sessions_tab()
        await wait_for(pilot, lambda: rows(app, "#sessions") == ["abcdef12", "0123abcd"], what="sessions")
        app.dashboard.query_one("#sessions").focus()
        await pilot.press("down", "enter")
        await job_done(pilot, app)
        assert w.boxen_calls()[0]["args"] == ["resume", "caulker", "0123abcd"]
        await pilot.press("x")
        await job_done(pilot, app, 2)
        assert w.boxen_calls()[1]["args"] == ["resume", "caulker", "0123abcd", "--stop"]
        await pilot.press("a")
        await wait_for(pilot, lambda: w.tmux_calls("new-window"), what="attach window")
        nw = w.tmux_calls("new-window")[0]
        assert nw[nw.index("-n") + 1] == "caulker/session-0123abcd"
        assert nw[-1] == hold_script([w.env["BOXEN_UI_BOXEN"], "resume", "caulker", "0123abcd", "--attach"])


@aio
async def test_sessions_when_stopped(world):
    w = world()
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("down")
        app.dashboard.action_sessions_tab()
        await wait_for(pilot, lambda: "isn't running" in screen_text(app), what="not running message")
        assert not any(c[:1] == ["exec"] for c in w.docker_calls())


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
    w = world(ls={"projects": [], "supervisor": None})
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
        await pilot.press("u")          # nothing selected: no crash, nothing run
        await pilot.pause(0.2)
        assert w.boxen_calls() == []


@aio
async def test_half_created_project_and_symlinked_status(world, tmp_path):
    w = world()
    secret = tmp_path / "app.pem"
    secret.write_text("-----BEGIN PRIVATE KEY----- SECRET")
    env = os.path.join(w.home, "projects", "caulker", "env")
    os.symlink(str(secret), os.path.join(env, "STATUS.md"))
    os.symlink(str(secret), os.path.join(env, "Dockerfile"))
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await wait_for(pilot, lambda: "env/STATUS.md is a symlink" in screen_text(app), what="refusal")
        assert "SECRET" not in screen_text(app)
        # e on a symlinked env file is refused, no editor window
        app.dashboard.query_one("#tabs").active = "files-tab"
        await pilot.pause(0.2)
        await pilot.press("e")
        await pilot.pause(0.3)
        assert w.tmux_calls("new-window") == []
        # beta has no env/STATUS.md and no container: fine
        await pilot.press("down")
        await pilot.pause(0.2)
        assert app.dashboard.selected == "beta"


@aio
async def test_edit_file_opens_a_private_copy(world):
    w = world()
    env_dir = os.path.join(w.home, "projects", "caulker", "env")
    with open(os.path.join(env_dir, "Dockerfile"), "w") as f:
        f.write("FROM boxen-base:latest\n")
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        app.dashboard.query_one("#tabs").active = "files-tab"
        await pilot.pause(0.2)
        await pilot.press("e")
        await wait_for(pilot, lambda: w.tmux_calls("new-window"), what="editor window")
        nw = w.tmux_calls("new-window")[0]
        assert nw[nw.index("-n") + 1] == "caulker/edit-Dockerfile"
        spec_path = nw[-1].split("boxen_ui.edit ")[1].split(";")[0].strip("'")
        spec = json.load(open(spec_path))
        # the editor never sees the real path: a private copy, mode 0600 in a 0700 folder
        assert spec["target"] == os.path.join(env_dir, "Dockerfile") and spec["copy"] != spec["target"]
        assert open(spec["copy"]).read() == "FROM boxen-base:latest\n"
        assert os.stat(spec["dir"]).st_mode & 0o777 == 0o700
        assert spec["editor"] == ["vim"]
        assert "-P -m boxen_ui.edit" in nw[-1]


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
    w.rules([{"args": ["update"], "out": ["building base image...", "caulker: rebuilt"], "delay": 0.8}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("g")
        await wait_for(pilot, lambda: isinstance(app.screen, GlobalMenuScreen), what="global menu")
        await pilot.press("enter")              # first option: update everything
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="confirm")
        await pilot.press("y")
        await wait_for(pilot, lambda: app.dashboard.jobs.running(), what="global job")
        await pilot.press("u", "r")
        await pilot.pause(0.2)
        assert [c["args"] for c in w.boxen_calls()] == [["update"]]
        await job_done(pilot, app)
        await pilot.press("u")
        await job_done(pilot, app, 2)
        assert [c["args"] for c in w.boxen_calls()] == [["update"], ["up", "caulker"]]


@aio
async def test_one_job_per_project(world):
    w = world()
    w.rules([{"args": ["up", "caulker"], "out": ["slow"], "sleep": 1.0}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("u", "r")
        await pilot.press("down", "u")          # another project: allowed
        await job_done(pilot, app, 2)
        assert sorted(c["args"][1] for c in w.boxen_calls()) == ["beta", "caulker"]
        assert ["restart", "caulker"] not in [c["args"] for c in w.boxen_calls()]


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
async def test_Q_stops_jobs_and_kills_session(world):
    w = world()
    w.rules([{"args": ["up", "caulker"], "out": ["working"], "sleep": 30}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("u")
        await wait_for(pilot, lambda: app.dashboard.jobs.jobs and app.dashboard.jobs.jobs[0].lines, what="job")
        pid = [c for c in w.boxen_calls()][0]["pid"]
        await pilot.press("Q")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="confirm")
        await pilot.press("y")
        await wait_for(pilot, lambda: w.tmux_calls("kill-session"), what="kill-session", timeout=10)
    assert w.tmux_calls("kill-session")[0] == ["-L", "boxen", "kill-session", "-t", "boxen"]
    assert not alive(pid)
    assert procs.live() == []


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
    assert not alive(pid)
    assert procs.live() == []


@aio
async def test_needs_terminal_offers_window(world):
    w = world()
    w.rules([{"args": ["new", "fresh"], "rc": 2, "out": [
        "boxen: login needs a terminal (Claude isn't signed in yet; run 'boxen login' on the VM)"]}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        app.dashboard.start_job(["new", "fresh"], project="fresh")
        await wait_for(pilot, lambda: isinstance(app.screen, QuestionScreen), what="question dialog")
        await press_button(pilot, app, "#terminal")
        await wait_for(pilot, lambda: w.tmux_calls("new-window"), what="window")
        nw = w.tmux_calls("new-window")[0]
        assert nw[nw.index("-n") + 1] == "fresh/new"
        assert nw[-1] == hold_script([w.env["BOXEN_UI_BOXEN"], "new", "fresh"])


@aio
async def test_command_palette_lists_actions(world):
    w = world()
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        titles = [c.title for c in app.get_system_commands(app.dashboard)]
        assert "Up: caulker" in titles and "Remove: caulker" in titles and "Self-update boxen" in titles
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
        nw = w.tmux_calls("new-window")[0]
        assert nw[nw.index("-n") + 1] == "+global/self-update"
        lock = app.cfg.global_lock
        assert nw[-1] == hold_script([w.env["BOXEN_UI_BOXEN"], "self-update"], lock=lock)
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
        assert "gh" not in cols
        assert "caulker" in screen_text(app)


# ------------------------------------------------------------ review round 1

@aio
async def test_restart_asks_and_refresh_keys(world):
    w = world()
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await pilot.press("r")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="restart asks")
        await pilot.press("n")
        await pilot.pause(0.3)
        assert w.boxen_calls() == []
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
    assert len(j.lines) == 10 and j.dropped == 15 and j.lines[0] == "line 15" and len(j.raw) == 10


@aio
async def test_unsupported_docker_endpoint(world):
    w = world()
    w.env["DOCKER_HOST"] = "tcp://10.0.0.5:2376"
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await wait_for(pilot, lambda: "docker unsupported endpoint (DOCKER_HOST)" in screen_text(app),
                       what="header says so")
        assert sum(w.server.counts.values()) == 0          # the default socket wasn't asked


def test_docker_endpoint_rules(tmp_path):
    from boxen_ui.config import docker_endpoint
    assert docker_endpoint({"DOCKER_HOST": "unix:///run/d.sock"}, str(tmp_path)) == ("/run/d.sock", None)
    assert docker_endpoint({"DOCKER_HOST": "ssh://vm"}, str(tmp_path))[0] is None
    assert docker_endpoint({"DOCKER_CONTEXT": "remote"}, str(tmp_path))[0] is None
    (tmp_path / ".docker").mkdir()
    (tmp_path / ".docker" / "config.json").write_text('{"currentContext": "colima"}')
    assert docker_endpoint({}, str(tmp_path)) == (None, "unsupported endpoint (docker context colima)")
    (tmp_path / ".docker" / "config.json").write_text('{"currentContext": "default"}')
    assert docker_endpoint({}, str(tmp_path)) == ("/var/run/docker.sock", None)


@aio
async def test_sessions_rows_split_dedupe_and_click(world):
    w = world()
    w.write("carry-caulker.tsv", "\n1\tabcdef12\t2026-10-01\t5\t1\t-\tsplit here\x85too\n\n"
                                 "2\tabcdef12\t2026-10-01\t5\t1\t-\tduplicate\n"
                                 "3\t0123abcd\t2026-09-01\t9\t2\tlive\tSecond\n")
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        app.dashboard.action_sessions_tab()
        await wait_for(pilot, lambda: rows(app, "#sessions") == ["abcdef12", "0123abcd"], what="sessions")
        assert "⏎ continue in app · a terminal · x close · ctrl+r refresh" in screen_text(app)
        await pilot.pause(0.2)
        # one click selects, nothing runs; a double click continues it
        await pilot.click("#sessions", offset=(4, 2))
        await pilot.pause(0.4)
        assert app.dashboard.query_one("#sessions").cursor_row == 1 and w.boxen_calls() == []
        await pilot.click("#sessions", offset=(4, 2), times=2)
        await job_done(pilot, app)
        assert w.boxen_calls()[0]["args"] == ["resume", "caulker", "0123abcd"]


@aio
async def test_sessions_columns_by_width(world):
    w = world()
    w.write("carry-caulker.tsv", "1\tabcdef12\t2026-10-01\t5\t1\tlive\t" + "A long title " * 8 + "\n")
    for size, want in (((200, 35), ["n", "state", "title", "turns", "parts", "date", "id"]),
                       ((140, 35), ["n", "state", "title", "turns", "parts", "date"]),
                       ((120, 35), ["n", "state", "title", "turns"]),
                       ((90, 28), ["n", "state", "title"])):
        app = BoxenApp(env=w.env)
        async with app.run_test(size=size) as pilot:
            await started(pilot, app)
            app.dashboard.action_sessions_tab()
            await wait_for(pilot, lambda: rows(app, "#sessions") == ["abcdef12"], what="sessions")
            t = app.dashboard.query_one("#sessions")
            assert [c.value for c in t.columns] == want
            assert t.get_cell("abcdef12", "title").plain.endswith("…")


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
    w = world(ls={"projects": [], "supervisor": None})
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await wait_for(pilot, lambda: app.dashboard.ls_loaded, what="ls")
        await pilot.pause(0.3)
        t = screen_text(app)
        assert "No projects yet" in t and "Moving from a Mac" in t and "README.md" in t
        for gone in ("cpu", "memory", "Sidecars", "Last apply", "STATUS.md"):
            assert gone not in t, gone
        assert "down (supervisor not set up)" in t


@aio
async def test_agent_down_amber_only_with_supervisor(world):
    w = world()
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        await wait_for(pilot, lambda: app.dashboard.sys.get("agent") is False, what="agent check")
        await pilot.pause(0.1)
        assert "agent down" in screen_text(app) and "supervisor not set up" not in screen_text(app)


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


def test_wrap_question_keeps_indent():
    out = wrap_question(["    " + "word " * 30, "      indented " + "x " * 40], width=40).split("\n")
    assert all(len(x) <= 40 for x in out)
    assert out[0].startswith("word") and out[1].startswith("    word")
    i = [n for n, x in enumerate(out) if x.startswith("  indented")][0]
    assert out[i + 1].startswith("      x")


@aio
async def test_edit_services_yaml_validates_then_offers_apply(world):
    w = world()
    env_dir = os.path.join(w.home, "projects", "caulker", "env")
    with open(os.path.join(env_dir, "services.yaml"), "w") as f:
        f.write("services: {}\n")
    app = BoxenApp(env=w.env)
    async with app.run_test(size=SIZE) as pilot:
        await started(pilot, app)
        app.dashboard.query_one("#tabs").active = "files-tab"
        await pilot.pause(0.2)
        app.dashboard.query_one("#files").focus()
        await pilot.pause(0.1)
        await pilot.press("down", "e")
        await wait_for(pilot, lambda: app.dashboard.pending_edits, what="edit pending")
        spec = list(app.dashboard.pending_edits.values())[0]
        assert spec["label"] == "env/services.yaml"
        # what the window does when vim exits
        with open(spec["copy"], "w") as f:
            f.write("services:\n  redis:\n    image: redis:7\n")
        from boxen_ui import edit
        res = edit.write_back(spec, open(spec["copy"], "rb").read())
        with open(spec["result"], "w") as f:
            json.dump(res, f)
        await wait_for(pilot, lambda: w.boxen_calls(), what="validation job")
        assert open(os.path.join(env_dir, "services.yaml")).read().startswith("services:\n  redis")
        assert w.boxen_calls()[0]["args"] == ["restart", "caulker", "--prepare"]
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="apply offer")
        await pilot.press("y")
        await job_done(pilot, app, 2)
        assert w.boxen_calls()[1]["args"] == ["restart", "caulker"]
        assert not os.path.exists(spec["dir"])                 # the private copy is gone


@aio
async def test_injection_harness(world):
    """The reviewer's harness: nothing from outside reaches the terminal
    as a control sequence, wherever it's shown."""
    import io
    import re as _re

    from rich.console import Console
    from conftest import sidecar
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
    c["server"] = "connected" + P
    c["repo"] = "r" + P
    c["container"]["id"] = P * 2
    c["sidecars"][0]["image"] = P
    c["sidecars"].append(sidecar("caulker", "x" + P, P))
    w = world(ls=doc)
    pdir = os.path.join(w.home, "projects", "caulker")
    open(os.path.join(pdir, "env", "STATUS.md"), "w").write("status " + P + "\nline2 " + P)
    open(os.path.join(pdir, "agent.log"), "w").write("apply " + P + "\n")
    PS = P.replace("\r", "")
    w.write("carry-caulker.tsv", "1\tabcdef12\t2026" + PS + "\t5" + PS + "\t1\tlive" + PS + "\tT" + PS + "\n")
    w.write("capture.txt", "cap " + P + "\n\x1b[31mred\x1b[0m\n")
    w.windows([{"id": "@0", "name": "dashboard"}, {"id": "@3", "name": "caulker/shell" + P}])
    w.rules([{"args": ["logs", "caulker", "--tail", "300"], "out": ["log " + P]},
             {"args": ["up", "caulker"], "out": ["job " + P, "boxen: approval needed (trust) for caulker:", "    q " + P,
                                                 "boxen: to approve, run: boxen up caulker --approve trust"], "rc": 3},
             {"args": ["update", "caulker"], "out": ["fail " + P], "rc": 1}])
    app = BoxenApp(env=w.env)
    async with app.run_test(size=(160, 45)) as pilot:
        await started(pilot, app)
        await pilot.pause(0.5)
        check(app, "overview")
        app.dashboard.action_sessions_tab()
        await wait_for(pilot, lambda: app.dashboard._sessions, what="sessions")
        await pilot.pause(0.2)
        check(app, "sessions")
        await pilot.press("l")
        await pilot.pause(0.6)
        check(app, "logs")
        app.dashboard.query_one("#tabs").active = "terminals"
        await pilot.pause(1.5)
        check(app, "terminals")
        await pilot.press("J", "U", "y")
        await job_done(pilot, app)
        await pilot.pause(0.3)
        check(app, "job-failed+notify")
        await pilot.press("u")
        await job_done(pilot, app, 2)
        await pilot.pause(0.5)
        check(app, type(app.screen).__name__)
        # the question had an escape in it: no approval dialog, a terminal offer instead
        assert app.dashboard.jobs.jobs[-1].approval is None
        assert isinstance(app.screen, QuestionScreen)
    assert not found, found


def test_long_lines_split_between_characters():
    from boxen_ui.jobs import FRAGMENT, SPLIT, LineSplitter, parse_approval
    out = []
    sp = LineSplitter(lambda text, frag: out.append((text, frag)))
    head = "é" * ((SPLIT + 1) // 2)                 # 2-byte characters across the cut
    run = "boxen: to approve, run: boxen up a --approve trust"
    data = ("short\n" + head + run + "\n" + run + "\n").encode()
    for i in range(0, len(data), 4096):
        sp.feed(data[i:i + 4096])
    sp.close()
    assert out[0] == ("short", False)
    pieces = [t for t, f in out[1:-1]]
    assert all(f for _t, f in out[1:-1]) and len(pieces) >= 2
    assert "\ufffd" not in "".join(pieces) and "".join(pieces) == head + run
    assert out[-1] == (run, False)
    # an over-long line is never a run line, even when its last piece reads like one
    raw = ["boxen: approval needed (trust) for a:", "    q"] + [FRAGMENT + t if f else t for t, f in out[1:-1]]
    assert parse_approval(raw, "a", "up") is None
    # a whole line longer than SPLIT arriving at once is a fragment too
    out.clear()
    sp = LineSplitter(lambda text, frag: out.append((text, frag)))
    sp.feed(b"x" * (SPLIT + 10) + b"\n")
    assert all(f for _t, f in out)
