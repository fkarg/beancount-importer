"""Tag menu — sub-prompt reached from Screen 1's `[t]` hotkey.

Lets the user set, change, or clear the run's active tag mid-flight.
The result lands on `CategoryProposal.tag_state_delta`; the pipeline
applies it before stamping the proposal's `tag` field, so picking
"set always" on this menu tags the current txn AND every subsequent
in-scope one.

Two ways to name the tag:
- Pick a **known tag** by letter (recent + ledger tags, see
  `CategorizeContext.known_tags`). The name is filled in; you still choose
  the mode. For a `duration` tag, the remembered window pre-fills the date
  prompt — beancount can't store it, so we carry it ourselves.
- Or choose a mode first (`1`/`2`/`3`) and type a fresh name.

Design doc rules: tag-menu hotkeys live behind the `[t]` boundary, so
their letters don't have to obey the top-level global reservations.
"""

from __future__ import annotations

from datetime import date as _date, datetime as _datetime

from rich.console import Console
from rich.prompt import Prompt

from beancount_importer.categorizer.screen import bottom_rule, hotkey
from beancount_importer.rules.tags import ActiveTag, RememberedTag, TagStateDelta


# Mode hotkeys for the menu. Numbered for muscle-memory consistency
# with other positional lists (Screen 2/4).
_MODE_HOTKEYS: tuple[str, ...] = ("1", "2", "3", "4", "5")

# Letters key the known-tag picker. Capped so the row stays readable and the
# letters never collide with the numeric mode hotkeys.
_LETTERS = "abcdefghij"
_MAX_PICKER = len(_LETTERS)


def render(
    console: Console,
    current: ActiveTag | None,
    picker: tuple[RememberedTag, ...] = (),
) -> None:
    """Render the tag-menu options. Pure I/O."""
    console.print()
    if current is None:
        console.print("  [bold]Tag menu[/]  [dim](no active tag)[/]")
    else:
        console.print(
            f"  [bold]Tag menu[/]  active: [magenta]{current.tag}[/] "
            f"[dim](mode={current.mode})[/]"
        )
    if picker:
        chips = "   ".join(
            f"{hotkey(letter)} {rt.tag}"
            for letter, rt in zip(_LETTERS, picker, strict=False)
        )
        console.print(f"    [dim]known:[/] {chips}")
    console.print(f"    {hotkey('1')} set once     (tag this txn only)")
    console.print(f"    {hotkey('2')} set always   (tag every subsequent txn)")
    console.print(f"    {hotkey('3')} set until    (tag through a date)")
    console.print(f"    {hotkey('4')} clear        (no tag from now on)")
    console.print(f"    {hotkey('5')} cancel       (no change)")
    bottom_rule(console)


def run(
    console: Console,
    current: ActiveTag | None,
    known: tuple[RememberedTag, ...] = (),
) -> TagStateDelta | None:
    """Render → prompt → return a delta (or None for cancel/no-change).

    `None` is distinct from `TagStateDelta(op="noop")`: returning None
    signals "the user backed out, do not record any change".

    No Enter default — the menu has no "obvious" choice (the user came here
    to *do* something), and silently cancelling on Enter would mirror the
    pager bug we fixed.
    """
    picker = tuple(known)[:_MAX_PICKER]
    render(console, current, picker)
    letters = _LETTERS[: len(picker)]
    key = Prompt.ask(
        ">",
        choices=[*letters, *_MODE_HOTKEYS],
        show_choices=False,
        show_default=False,
    ).strip()
    if key == "5":
        return None
    if key == "4":
        return TagStateDelta(op="clear")
    if key in letters:
        # Known-tag pick: name is set; the user still chooses the mode, and a
        # remembered window pre-fills the `until` prompt.
        chosen = picker[letters.index(key)]
        mode_key = _prompt_mode(chosen.tag)
        return _build(console, chosen.tag, mode_key, chosen.until_date)
    # Modes 1/2/3 with a fresh, typed name.
    name = _prompt_tag_name()
    if not name:
        return None
    return _build(console, name, key, default_until=None)


def _build(
    console: Console,
    name: str,
    mode_key: str,
    default_until: _date | None,
) -> TagStateDelta | None:
    """Turn a (name, mode) choice into a set-delta, prompting for a date on
    `until`. `default_until` pre-fills that prompt when re-picking a tag."""
    if mode_key == "1":
        return TagStateDelta(op="set", new_state=ActiveTag(tag=name, mode="once"))
    if mode_key == "2":
        return TagStateDelta(op="set", new_state=ActiveTag(tag=name, mode="always"))
    until = _prompt_until_date(console, default_until)
    if until is None:
        return None
    return TagStateDelta(
        op="set", new_state=ActiveTag(tag=name, mode="duration", until_date=until)
    )


def _prompt_mode(name: str) -> str:
    """Mode pick for an already-chosen tag name."""
    return Prompt.ask(
        f"mode for [magenta]{name}[/] [dim](1 once / 2 always / 3 until)[/]",
        choices=["1", "2", "3"],
        show_choices=False,
        show_default=False,
    ).strip()


def _prompt_tag_name() -> str:
    """Free-text tag name. Empty input cancels."""
    return Prompt.ask("tag name [dim](empty cancels)[/]", default="").strip()


def _prompt_until_date(console: Console, default: _date | None) -> _date | None:
    """ISO-format date prompt, defaulting to a remembered window when re-picking.

    Accepts `YYYY-MM-DD`. Anything else (including empty with no default)
    returns None so the user can back out without committing a bad date.
    """
    raw = Prompt.ask(
        "until date [dim](YYYY-MM-DD; empty cancels)[/]",
        default=default.isoformat() if default else "",
    ).strip()
    if not raw:
        return None
    try:
        return _datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        console.print(f"  [yellow]invalid date {raw!r} — cancelling[/]")
        return None
