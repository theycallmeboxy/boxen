"""Modal dialogs. Text from outside (boxen's output, questions) is shown as
Text objects, never markup."""
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
    _Dialog, ConfirmScreen, NewProjectScreen, GlobalMenuScreen, HelpScreen, ReposScreen {
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


class NewProjectScreen(_Dialog):
    """Name and optional GitHub URL, checked like bin/boxen does. Dismisses
    (name, url) or None."""

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text("New project"), classes="title")
            yield Label(Text("Name (becomes ~/src/<name>)"))
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
            yield Static(Text("Its tokens cover only these: the agent can push to every one. "
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
    ("update_all", "Update everything", "rebuild the base image (fresh Ubuntu) and every project's build container"),
    ("self_update", "Self-update boxen", "pull the latest boxen and apply it (in a terminal: may ask for sudo)"),
    ("tokens", "Refresh GitHub tokens", "mint fresh tokens for every project now"),
    ("github", "Connect GitHub", "set up your GitHub App and commit identity (in a terminal)"),
    ("doctor", "Check this VM", "boxen doctor: rootless Docker, the sandbox and isolation"),
    ("migrate", "Move from boxen v1", "boxen migrate: bring v1 projects (agent in a container) over (in a terminal)"),
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
        ("c", "Claude: switch to the project's session, or start one (terminal window)"),
        ("C", "a new Claude session in the project, beside any running"),
        ("S", "serve: keep a Remote Control server up, to start sessions from the app (on / off)"),
        ("u", "up: build and start the build container and sidecars, applying .boxen/ changes"),
        ("d", "down: stop them (asks first)"),
        ("s", "root shell in the build container (terminal window)"),
        ("v", "shell on the VM in the project folder (terminal window); the folder is the agent's: "
              "git there won't start a repo's fsmonitor, but its hooks still run when you commit"),
        ("n", "new project"),
        ("G", "GitHub repos the project can push to: add, remove"),
        ("X", "remove the build container, image and data (asks first; keeps the code)"),
    ]),
    ("Tabs", [
        ("l", "Logs tab"),
        ("ctrl+r  F5", "refresh (the list, and the tab shown)"),
        ("Enter", "Sessions: switch to the window"),
        ("x", "Sessions: close the window (asks first)"),
        ("f", "Logs: follow a sidecar's log in a terminal window"),
        ("a", "Overview: add the selected found repo (asks)"),
        ("e", "Files: edit a copy in vim; saved back when you quit vim"),
    ]),
    ("Everything", [
        ("g", "global menu: update all, self-update, tokens, GitHub, doctor, migrate"),
        ("J", "show / hide the jobs panel"),
        ("ctrl+p", "command palette: every action"),
        ("F1  C-b 0", "back to this dashboard (from any window)"),
        ("F2 F3", "previous / next window (or C-b p / C-b n)"),
        ("q", "detach: everything keeps running; `boxen ui` comes back"),
        ("Q  ctrl+q", "quit the dashboard (asks); sessions and other windows keep running"),
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
        t.append("Claude sessions and other windows run in boxen's tmux: switch with the tabs at the "
                 "bottom, F1 (or C-b 0, when a program in the window wants the F-keys) comes back here. "
                 "Everything keeps running if SSH drops.", style="dim")
        with Vertical(classes="dialog"):
            yield Static(Text("boxen ui keys"), classes="title")
            with VerticalScroll(classes="body"):
                yield Static(t)
