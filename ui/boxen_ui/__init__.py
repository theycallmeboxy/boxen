"""boxen ui: a terminal dashboard for boxen, run inside its own tmux server.

Started by `boxen ui` (bin/boxen), which runs `python -m boxen_ui` in the
dashboard window of the `boxen` tmux session, beside the projects' Claude
sessions. Everything it changes goes through `boxen` itself (stdin from
/dev/null, so nothing waits on a prompt), or happens in a tmux window: a
Claude session, a shell, the editor, a command that needs a terminal.
"""
