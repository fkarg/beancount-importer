"""Shared rendering primitives for every categorizer screen.

Extracted from `confirm.py` and `collision.py` after both prototypes
proved out — the pieces here are exactly the duplications that surfaced.
Deliberately minimal: a markup helper, an account-styling helper, the
horizontal rule, and a `Prompt.ask` wrapper. No `Screen` class — every
rendering function in a screen module already takes a `Console`, so an
extra wrapper would only add indirection.
"""

from __future__ import annotations

import os
import sys
import termios
import tty
from collections.abc import Callable
from datetime import date

from rich.console import Console
from rich.prompt import Prompt

from beancount_importer.matching.account_suggest import account_glyph
from beancount_importer.rules.tags import ActiveTag


# Rule width matches the design doc's screen examples (73 columns). All
# screens render this exact width so the visual rhythm is consistent.
RULE_WIDTH = 73
RULE = "─" * RULE_WIDTH


def hotkey(letter: str) -> str:
    """Render `[<letter>]` as cyan literal text.

    Rich's markup parser treats `[…]` as a tag opener, so the literal
    bracket needs an escape (`\\[`). `[/]` closes the surrounding cyan
    style. Used in every screen's hotkey row.
    """
    return rf"[cyan]\[{letter}][/]"


def styled_account(account: str) -> str:
    """Glyph + space + account name, wrapped in the class's Rich style.

    Callers replace empty strings with their own placeholder before
    passing in (different screens want different placeholders, e.g.
    `?` vs `(none)`), so this helper assumes a non-empty name.
    """
    glyph, style = account_glyph(account)
    return f"[{style}]{glyph} {account}[/]"


def bottom_rule(console: Console) -> None:
    """Print the bottom horizontal rule that closes every screen."""
    console.print(RULE)


# Rich's `Prompt.ask(">")` renders the prompt as `>: ` (prompt + the
# default `": "` suffix). The raw single-key path reproduces that string
# byte-for-byte so switching paths is visually invisible.
_PROMPT = ">: "

_CTRL_L = b"\x0c"
_ENTER_KEYS = (b"\r", b"\n")
# Empty read = stream closed; `\x04` = Ctrl-D. Both mean end-of-input,
# which `Prompt.ask` surfaces as `EOFError`; the raw path matches.
_EOF_KEYS = (b"", b"\x04")


def _classify_key(byte: bytes, valid: frozenset[str]) -> tuple[str, str]:
    """Map one raw input byte to a `(kind, value)` action. Pure.

    `kind` is `"key"` (return `value`), `"redraw"` (Ctrl-L pressed), or
    `"ignore"` (not one of `valid` — swallow it, as `Prompt.ask`'s choice
    list would). Raises `EOFError` on end-of-input. Enter (CR/LF) maps to
    the empty-string key, the design's "Enter is a hotkey" convention.
    """
    if byte in _EOF_KEYS:
        raise EOFError
    if byte == _CTRL_L:
        return ("redraw", "")
    key = "" if byte in _ENTER_KEYS else byte.decode("latin-1")
    if key in valid:
        return ("key", key)
    return ("ignore", "")


def _read_hotkey(
    choices: tuple[str, ...], console: Console, redraw: Callable[[], None]
) -> str:
    """Single-keypress prompt that honours Ctrl-L (clear + re-render).

    Reads one byte at a time in cbreak mode so Ctrl-L can clear the
    screen and redraw the current screen mid-prompt — something
    `Prompt.ask`'s line-buffered `input()` cannot do (it only returns a
    whole line, with no hook to run our `redraw` on a keystroke). Only
    reached on a real terminal; the piped/test path stays on `Prompt.ask`
    (see `ask_hotkey`). cbreak (not raw) keeps `ISIG` on, so Ctrl-C still
    raises `KeyboardInterrupt`, and `OPOST` on, so `\\n` still expands to
    `\\r\\n` for the Rich output below.
    """
    valid = frozenset(choices)
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        # `TCSANOW`, not the `setcbreak` default `TCSAFLUSH`: don't discard
        # bytes already queued — `input()` didn't either, and flushing would
        # drop a key the user typed just before the prompt repainted.
        tty.setcbreak(fd, termios.TCSANOW)
        while True:
            console.print(_PROMPT, end="")
            while True:
                kind, value = _classify_key(os.read(fd, 1), valid)
                if kind == "redraw":
                    console.clear()
                    redraw()
                    break  # back to the outer loop → reprint the prompt
                if kind == "ignore":
                    continue
                console.print(value)  # echo the accepted key + newline
                return value
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def ask_hotkey(
    choices: tuple[str, ...],
    *,
    console: Console | None = None,
    redraw: Callable[[], None] | None = None,
) -> str:
    """Prompt for a single hotkey from `choices`. Empty input means Enter.

    The empty string `""` MUST appear in `choices` for Enter to be
    accepted — the design doc treats Enter as one of the hotkeys, not
    a separate input mode.

    On a real terminal, when `console` and `redraw` are supplied, reads a
    single keypress directly so Ctrl-L can clear and re-render the current
    screen (`redraw` re-runs that screen's render). Otherwise — piped
    input, or tests, which monkeypatch `Prompt.ask` — falls back to
    `Prompt.ask`, whose choice list enforces `choices` for us.
    """
    if console is not None and redraw is not None and sys.stdin.isatty():
        return _read_hotkey(choices, console, redraw)
    return Prompt.ask(
        ">",
        choices=list(choices),
        default="",
        show_choices=False,
        show_default=False,
    ).strip()


def tag_remaining_days(tag: ActiveTag | None, booking_date: date) -> int | None:
    """Days left for a `duration`-mode active tag; None for other modes.

    A UI concern (the header's `(N left)` suffix), kept off `ActiveTag`
    which is shared across persistence/serialisation. Clamps at 0 so an
    expired tag never renders a negative count. Shared by the header
    helpers in `host` and the `[t]` re-render in `confirm`.
    """
    if tag is None or tag.mode != "duration" or tag.until_date is None:
        return None
    return max(0, (tag.until_date - booking_date).days)
