"""Tag menu — sub-prompt reached from Screen 1's `[t]` hotkey.

Lets the user set, change, or clear the run's active tag mid-flight.
The result lands on `CategoryProposal.tag_state_delta`; the pipeline
applies it before stamping the proposal's `tag` field, so picking
"set always" on this menu tags the current txn AND every subsequent
in-scope one.

Design doc rules: tag-menu hotkeys live behind the `[t]` boundary, so
their letters don't have to obey the top-level global reservations.
We use numbered keys (1–5) for the mode pick, then a free-text tag
prompt and (for `until`) a date prompt.
"""

from __future__ import annotations

from datetime import date as _date, datetime as _datetime

from rich.console import Console
from rich.prompt import Prompt

from beancount_importer.categorizer.screen import bottom_rule, hotkey
from beancount_importer.rules.tags import ActiveTag, TagStateDelta


# Mode hotkeys for the menu. Numbered for muscle-memory consistency
# with other positional lists (Screen 2/4); the design doc treats
# numbers as positional everywhere.
_HOTKEYS: tuple[str, ...] = ("1", "2", "3", "4", "5")


def render(console: Console, current: ActiveTag | None) -> None:
    """Render the tag-menu options. Pure I/O."""
    console.print()
    if current is None:
        console.print("  [bold]Tag menu[/]  [dim](no active tag)[/]")
    else:
        console.print(
            f"  [bold]Tag menu[/]  active: [magenta]{current.tag}[/] "
            f"[dim](mode={current.mode})[/]"
        )
    console.print(f"    {hotkey('1')} set once     (tag this txn only)")
    console.print(f"    {hotkey('2')} set always   (tag every subsequent txn)")
    console.print(f"    {hotkey('3')} set until    (tag through a date)")
    console.print(f"    {hotkey('4')} clear        (no tag from now on)")
    console.print(f"    {hotkey('5')} cancel       (no change)")
    bottom_rule(console)


def run(console: Console, current: ActiveTag | None) -> TagStateDelta | None:
    """Render → prompt → return a delta (or None for cancel/no-change).

    `None` is distinct from `TagStateDelta(op="noop")`: returning None
    signals "the user backed out, do not record any change". A noop
    would still flow through the proposal and replay.
    """
    render(console, current)
    key = Prompt.ask(
        ">",
        choices=list(_HOTKEYS),
        default="5",
        show_choices=False,
        show_default=False,
    ).strip()
    if key == "5":
        return None
    if key == "4":
        return TagStateDelta(op="clear")
    # Modes 1/2/3 all need a tag name.
    name = _prompt_tag_name(console)
    if not name:
        return None
    if key == "1":
        return TagStateDelta(op="set", new_state=ActiveTag(tag=name, mode="once"))
    if key == "2":
        return TagStateDelta(op="set", new_state=ActiveTag(tag=name, mode="always"))
    # key == "3" — duration
    until = _prompt_until_date(console)
    if until is None:
        return None
    return TagStateDelta(
        op="set", new_state=ActiveTag(tag=name, mode="duration", until_date=until)
    )


def _prompt_tag_name(console: Console) -> str:
    """Free-text tag name. Empty input cancels."""
    del console  # `Prompt.ask` writes to the global console implicitly
    return Prompt.ask(
        "tag name [dim](empty cancels)[/]", default=""
    ).strip()


def _prompt_until_date(console: Console) -> _date | None:
    """ISO-format date prompt. Invalid input cancels with a brief warning.

    Accepts `YYYY-MM-DD`. Anything else (including empty) returns None
    so the user can back out without committing a malformed date.
    """
    raw = Prompt.ask(
        "until date [dim](YYYY-MM-DD; empty cancels)[/]", default=""
    ).strip()
    if not raw:
        return None
    try:
        return _datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        console.print(f"  [yellow]invalid date {raw!r} — cancelling[/]")
        return None
