"""Modal dialogs. Text from outside (boxen's output, questions) is shown as
Text objects, never markup."""
import textwrap

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from .config import name_problem, repo_arg, repo_problem, url_problem
from .model import GREY, repo_state
from .textutil import clean_line, text


class _Dialog(ModalScreen):
    DEFAULT_CSS = """
    _Dialog, ConfirmScreen, ApprovalScreen, QuestionScreen, NewProjectScreen, GlobalMenuScreen, HelpScreen,
    ReposScreen {
        align: center middle;
        background: $background 60%;
    }
    .dialog {
        width: 84;
        max-width: 95%;
        height: auto;
        max-height: 90%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    .dialog .title { text-style: bold; margin-bottom: 1; }
    .dialog .body { height: auto; max-height: 20; }
    .dialog .block { background: $panel; padding: 0 1; height: auto; }
    .dialog .cmd { margin-top: 1; color: $text-muted; }
    .dialog .buttons { height: auto; margin-top: 1; align-horizontal: right; }
    .dialog .buttons Button { margin-left: 2; min-width: 10; }
    .dialog .error { color: $error; height: auto; }
    .dialog Input { margin-bottom: 1; }
    .dialog OptionList { height: auto; max-height: 14; }
    .dialog.warn { border: thick $warning; }
    """
    BINDINGS = [Binding("escape", "dismiss_none", "Close", show=False)]

    def action_dismiss_none(self):
        self.dismiss(None)


class ConfirmScreen(_Dialog):
    """Yes/No. Dismisses True or False."""
    BINDINGS = [Binding("y", "yes", "Yes"), Binding("n,escape", "no", "No")]

    def __init__(self, title, message, yes="Yes", warn=False):
        super().__init__()
        self._title, self._message, self._yes, self._warn = title, message, yes, warn

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog warn" if self._warn else "dialog"):
            yield Static(Text(self._title), classes="title")
            yield Static(self._message if isinstance(self._message, Text) else text(self._message), classes="body")
            with Horizontal(classes="buttons"):
                yield Button(self._yes, id="yes", variant="error" if self._warn else "primary")
                yield Button("No", id="no")

    def on_mount(self):
        self.query_one("#no", Button).focus()

    def on_button_pressed(self, ev):
        self.dismiss(ev.button.id == "yes")

    def action_yes(self):
        self.dismiss(True)

    def action_no(self):
        self.dismiss(False)


def wrap_question(lines, width=72):
    """The question's lines (as printed, indented by 4) without that
    indent, wrapped at word boundaries; a wrapped line's continuation is
    indented 4 more than the line itself."""
    out = []
    for line in lines:
        line = clean_line(line[4:] if line.startswith("    ") else line).rstrip()
        lead = line[:len(line) - len(line.lstrip())]
        out.extend(textwrap.wrap(line.lstrip(), width=width, initial_indent=lead, subsequent_indent=lead + "    ",
                                 break_on_hyphens=False) or [""])
    return "\n".join(out)


class ApprovalScreen(_Dialog):
    """The question from an "approval needed" block (boxen's own words,
    wrapped) and the exact command Yes runs. No has the focus. Yes
    dismisses True: the caller runs exactly that command."""
    DEFAULT_CSS = """
    ApprovalScreen #yes { background: $surface; color: $warning; border: tall $warning; }
    ApprovalScreen #yes:focus { background: $warning; color: $background; text-style: bold; }
    ApprovalScreen #no:focus { background: $accent; color: $background; text-style: bold reverse; border: tall $accent; }
    """
    BINDINGS = [Binding("n,escape", "no", "No")]

    def __init__(self, approval, job_title):
        super().__init__()
        self.approval = approval
        self.job_title = job_title

    def compose(self) -> ComposeResult:
        a = self.approval
        with Vertical(classes="dialog warn"):
            yield Static(Text("%s needs your approval" % clean_line(a.name)), classes="title")
            yield Static(Text("%s stopped to ask (%s):" % (clean_line(self.job_title), a.kind), style="dim"))
            with VerticalScroll(classes="body"):
                yield Static(Text(wrap_question(a.question), no_wrap=True, overflow="ellipsis"),
                             classes="block", id="approval-block")
            yield Static(Text.assemble(("Yes runs: ", "bold"), clean_line(a.command)), classes="cmd", id="approval-cmd")
            with Horizontal(classes="buttons"):
                yield Button("Yes, approve", id="yes")
                yield Button("No", id="no", variant="primary")

    def on_mount(self):
        self.query_one("#no", Button).focus()

    def on_button_pressed(self, ev):
        self.dismiss(ev.button.id == "yes")

    def action_no(self):
        self.dismiss(False)


class QuestionScreen(_Dialog):
    """A job stopped at something only a terminal can answer. Dismisses
    "terminal" or None."""

    def __init__(self, title, lines, can_open=True, button="Open a terminal for it"):
        super().__init__()
        self._title, self._lines, self._can_open, self._button = title, lines, can_open, button

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog warn"):
            yield Static(Text(self._title), classes="title")
            with VerticalScroll(classes="body"):
                yield Static(Text("\n".join(clean_line(x) for x in self._lines)), classes="block", id="question-text")
            with Horizontal(classes="buttons"):
                if self._can_open:
                    yield Button(self._button, id="terminal", variant="primary")
                yield Button("Close", id="close")

    def on_mount(self):
        self.query_one("#terminal" if self._can_open else "#close", Button).focus()

    def on_button_pressed(self, ev):
        self.dismiss("terminal" if ev.button.id == "terminal" else None)


class NewProjectScreen(_Dialog):
    """Name and optional GitHub URL, checked like bin/boxen does. Dismisses
    (name, url) or None."""

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text("New project"), classes="title")
            yield Label(Text("Name (becomes ~/src/<name> and the name in the Claude app)"))
            yield Input(placeholder="myproject", id="name", max_length=40)
            yield Label(Text("GitHub URL to clone (optional, https://github.com/owner/repo)"))
            yield Input(placeholder="https://github.com/you/myproject", id="url")
            yield Static("", classes="error", id="error")
            with Horizontal(classes="buttons"):
                yield Button("Create", id="create", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self):
        self.query_one("#name", Input).focus()

    def _submit(self):
        name = self.query_one("#name", Input).value.strip()
        url = self.query_one("#url", Input).value.strip()
        why = name_problem(name)
        if why:
            self.query_one("#error", Static).update(Text("Name: " + why))
            self.query_one("#name", Input).focus()
            return
        why = url_problem(url)
        if why:
            self.query_one("#error", Static).update(Text("URL: " + why))
            self.query_one("#url", Input).focus()
            return
        self.dismiss((name, url))

    def on_input_submitted(self, ev):
        if ev.input.id == "name":
            self.query_one("#url", Input).focus()
        else:
            self._submit()

    def on_button_pressed(self, ev):
        if ev.button.id == "create":
            self._submit()
        else:
            self.dismiss(None)


class ReposScreen(_Dialog):
    """A project's GitHub repos (G): the list with each repo's state, the
    ones found in its folder but not listed, a field to add more (checked
    like bin/boxen does) and x to remove the selected one. Dismisses ("add",
    [owner/repo, ...]), ("rm", [owner/repo]) or None. Every repo name is
    shown as plain text: the found ones come from the project's own git
    config."""
    DEFAULT_CSS = """
    ReposScreen #repo-list { height: auto; max-height: 12; margin-bottom: 1; }
    """
    BINDINGS = [Binding("x", "remove", "Remove"), Binding("escape", "dismiss_none", "Close")]

    def __init__(self, project, repos, found):
        super().__init__()
        self.project = project
        self.repos = list(repos)          # model.Repo
        self.found = list(found)          # str
        self.rows = []                    # ("listed" | "found", repo), by row

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text("GitHub repos for %s" % clean_line(self.project)), classes="title")
            yield Static(Text("Its tokens cover only these. Adding asks you to approve it; "
                              "x removes the selected one; Enter on a found one puts it below.", style="dim"))
            yield DataTable(id="repo-list", cursor_type="row")
            yield Label(Text("Add (owner/repo or https://github.com/owner/repo; several: spaces between)"))
            yield Input(placeholder="you/fork", id="repo-input")
            yield Static("", classes="error", id="error")
            with Horizontal(classes="buttons"):
                yield Button("Add", id="add", variant="primary")
                yield Button("Close", id="close")

    def on_mount(self):
        t = self.query_one("#repo-list", DataTable)
        for label, key in (("REPO", "repo"), ("STATE", "state"), ("", "mark")):
            t.add_column(Text(label, style="bold"), key=key)
        for r in self.repos:
            label, style = repo_state(r.state)
            t.add_row(text(r.repo), Text(clean_line(label), style=style), Text("origin" if r.origin else "", style=GREY),
                      key="r%d" % len(self.rows))
            self.rows.append(("listed", r.repo))
        for f in self.found:
            t.add_row(text(f), Text("found, not listed", style="bold #f5a524"),
                      Text("" if repo_arg(f) else "not a repo name", style=GREY), key="r%d" % len(self.rows))
            self.rows.append(("found", f))
        if not self.rows:
            t.add_row(Text("none yet", style=GREY), Text(""), Text(""), key="none")
        self.query_one("#repo-input", Input).focus()

    def _selected(self):
        t = self.query_one("#repo-list", DataTable)
        if not self.rows or t.row_count == 0:
            return None
        i = t.cursor_row
        return self.rows[i] if 0 <= i < len(self.rows) else None

    def on_data_table_row_selected(self, ev):
        sel = self._selected()
        if sel and sel[0] == "found" and repo_arg(sel[1]):
            self.query_one("#repo-input", Input).value = repo_arg(sel[1])
            self.query_one("#repo-input", Input).focus()

    def action_remove(self):
        if isinstance(self.focused, Input):
            return
        sel = self._selected()
        if sel is None or sel[0] != "listed":
            self.query_one("#error", Static).update(Text("Select a listed repo to remove it."))
            return
        self.dismiss(("rm", [sel[1]]))

    def _submit(self):
        words = self.query_one("#repo-input", Input).value.replace(",", " ").split()
        if not words:
            self.query_one("#error", Static).update(Text("Type a repo to add: owner/repo"))
            return
        out, seen = [], set()
        for w in words:
            why = repo_problem(w)
            if why:
                self.query_one("#error", Static).update(Text(why))
                return
            r = repo_arg(w)
            if r.lower() not in seen:
                seen.add(r.lower())
                out.append(r)
        self.dismiss(("add", out))

    def on_input_submitted(self, ev):
        self._submit()

    def on_button_pressed(self, ev):
        if ev.button.id == "add":
            self._submit()
        else:
            self.dismiss(None)


GLOBAL_ACTIONS = [
    ("update_all", "Update everything", "rebuild the base image (fresh Ubuntu, newest Claude Code) and every project"),
    ("self_update", "Self-update boxen", "pull the latest boxen and apply it (in a terminal: may ask for sudo)"),
    ("tokens", "Refresh GitHub tokens", "mint fresh tokens for every project now"),
    ("github", "Connect GitHub", "set up your GitHub App and commit identity (in a terminal)"),
    ("login", "Sign in to Claude", "sign in again, shared by every project (in a terminal)"),
    ("import_mac", "Moving from a Mac", "how to bring your Claude Code work over (in a terminal)"),
    ("supervisor", "Set up the supervisor", "create or start the supervisor project (in a terminal)"),
]


class GlobalMenuScreen(_Dialog):
    """Dismisses the chosen action's key or None."""

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text("Everything (g)"), classes="title")
            yield OptionList(*[Option(Text.assemble((title, "bold"), "\n", (desc, "dim")), id=key)
                               for key, title, desc in GLOBAL_ACTIONS], id="global-options")

    def on_mount(self):
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, ev):
        self.dismiss(ev.option.id)


HELP = [
    ("Projects", [
        ("↑ ↓  j k", "move in the project list"),
        ("Tab", "next pane"),
        ("u", "up: start, or apply env changes"),
        ("d", "down: stop (asks first)"),
        ("r", "restart: recreate the container (asks first)"),
        ("U", "update: rebuild the image (asks first)"),
        ("s", "shell in the container (terminal window)"),
        ("c", "Claude in the container (terminal window)"),
        ("n", "new project"),
        ("G", "GitHub repos the project can push to: add (asks), remove"),
        ("X", "remove the project (asks first; keeps the code)"),
    ]),
    ("Tabs", [
        ("l", "Logs tab"),
        ("ctrl+r  F5", "refresh (the list, and the Sessions / Logs tab)"),
        ("f", "Logs: follow in a terminal window"),
        ("Enter", "Sessions: continue in the Claude app · Terminals: switch to it"),
        ("a", "Overview: add the selected found repo (asks) · Sessions: open in a terminal"),
        ("x", "Sessions: close one boxen resumed"),
        ("e", "Files: edit a copy in vim; saved back when you quit vim"),
    ]),
    ("Everything", [
        ("g", "global menu: update all, self-update, tokens, GitHub, login…"),
        ("J", "show / hide the jobs panel"),
        ("ctrl+p", "command palette: every action"),
        ("F1  C-b 0", "back to this dashboard (from any window)"),
        ("F2 F3", "previous / next window (or C-b p / C-b n)"),
        ("q", "detach: everything keeps running; `boxen ui` comes back"),
        ("Q  ctrl+q", "quit: stop jobs and close every boxen ui window (asks)"),
        ("?", "this help"),
    ]),
]


class HelpScreen(_Dialog):
    BINDINGS = [Binding("escape,q,question_mark", "dismiss_none", "Close")]

    def compose(self) -> ComposeResult:
        t = Text()
        for section, keys in HELP:
            t.append(section + "\n", style="bold underline")
            for k, desc in keys:
                t.append("  %-12s" % k, style="bold cyan")
                t.append(desc + "\n")
            t.append("\n")
        t.append("Terminal windows (shell, claude, logs, sessions) run in tmux: switch with the tabs at the "
                 "bottom, F1 (or C-b 0, when a program in the window wants the F-keys) comes back here. "
                 "Everything keeps running if SSH drops.", style="dim")
        with Vertical(classes="dialog"):
            yield Static(Text("boxen ui keys"), classes="title")
            with VerticalScroll(classes="body"):
                yield Static(t)
