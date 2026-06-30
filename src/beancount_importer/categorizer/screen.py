"""Shared rendering primitives for every categorizer screen.

Extracted from `confirm.py` and `collision.py` after both prototypes
proved out — the pieces here are exactly the duplications that surfaced.
Deliberately minimal: a markup helper, an account-styling helper, the
horizontal rule, and a `Prompt.ask` wrapper. No `Screen` class — every
rendering function in a screen module already takes a `Console`, so an
extra wrapper would only add indirection.
"""

from __future__ import annotations

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


def ask_hotkey(choices: tuple[str, ...]) -> str:
    """Prompt for a single hotkey from `choices`. Empty input means Enter.

    The empty string `""` MUST appear in `choices` for Enter to be
    accepted — the design doc treats Enter as one of the hotkeys, not
    a separate input mode. `Prompt.ask` enforces the choices list, so
    callers don't need to validate the return value.
    """
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
