"""boxen ui: a terminal dashboard for boxen, run inside its own tmux server.

Started by `boxen ui` (bin/boxen), which runs `python -m boxen_ui` in the
dashboard window of the `boxen` tmux session. Everything it does goes
through `boxen` itself (with BOXEN_AGENT=1, so nothing waits on a prompt)
or opens a tmux window for commands that need a terminal.
"""
