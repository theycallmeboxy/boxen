"""Untrusted text in, plain text out.

Everything shown in the UI that came from somewhere else (logs, session
titles, files, Docker, boxen's output) goes through here: terminal control
sequences and control characters are removed, and it's wrapped in a Rich
Text, which is never parsed as markup. The one exception is a tmux pane
capture, rendered with its colors: sgr_only keeps the color codes (SGR) and
drops every other sequence, including OSC 8 hyperlinks, before
Text.from_ansi sees it.
"""
import re

from rich.text import Span, Text

# CSI (colors, cursor, clears), OSC (titles, hyperlinks), DCS/APC/PM/SOS
# strings, charset selection, other two-byte escapes.
_ESC = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[P_^X][^\x1b]*(?:\x1b\\)?"
    r"|\x1b[()*+][0-9A-Za-z]"
    r"|\x1b[ -/]*[0-~]?"
)
_SGR = re.compile(r"\x1b\[[0-9;:]*m")
# C0 except \t and \n, DEL, C1 (an 8-bit CSI is \x9b), and the Unicode
# direction overrides that can make text read differently from what it is.
_CTRL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")


def clean_line(s):
    """One line of untrusted output as plain text: escapes and control
    characters removed; a carriage return keeps what was drawn last."""
    s = _ESC.sub("", s)
    if "\r" in s:
        parts = [p for p in s.split("\r") if p]
        s = parts[-1] if parts else ""
    s = s.replace("\t", "    ")
    return _CTRL.sub("", s)


def clean(s):
    """Multi-line untrusted text, line by line."""
    return "\n".join(clean_line(line) for line in s.split("\n"))


def text(s, style=""):
    """A Text of untrusted s (never markup)."""
    return Text(clean_line(s) if "\n" not in s else clean(s), style=style)


def sgr_only(s):
    """A tmux capture with only its color codes left: every other escape
    (and control character but \\n) is removed."""
    out = []
    pos = 0
    for m in _SGR.finditer(s):
        out.append(_strip_all(s[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_strip_all(s[pos:]))
    return "".join(out)


def _strip_all(s):
    s = _ESC.sub("", s).replace("\t", "    ").replace("\r", "")
    return _CTRL.sub("", s)


def ansi_text(capture):
    """A tmux capture (capture-pane -e) as colored Text, SGR only."""
    t = Text.from_ansi(sgr_only(capture))
    # from_ansi only makes links from OSC 8, which sgr_only removed; drop
    # any link style regardless.
    t.spans = [Span(sp.start, sp.end, sp.style.update_link(None))
               if not isinstance(sp.style, str) and sp.style.link else sp
               for sp in t.spans]
    return t
