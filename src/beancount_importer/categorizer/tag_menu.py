"""Tag menu — sub-prompt reached from Screen 1's `[t]` hotkey.

Lets the user set, change, or clear the run's active tag mid-flight.
The result lands on `CategoryProposal.tag_state_delta`; the pipeline
applies it before stamping the proposal's `tag` field, so picking
"always" on this menu tags the current txn AND every subsequent
in-scope one.

Two-step flow, one axis per screen:
1. **Pick a tag** — a known tag by letter (recent + ledger tags, see
   `CategorizeContext.known_tags`), or `[n]` to type a fresh name, or
   `[c]` clear / `[.]` cancel.
2. **Pick a mode** — `once` / `always` / `until` for the chosen name. A
   `duration` (`until`) tag then prompts for a date; a remembered window
   pre-fills it (beancount can't store it, so we carry it ourselves).

Design doc rules: tag-menu hotkeys live behind the `[t]` boundary, so
their letters don't have to obey the top-level global reservations.
"""

from __future__ import annotations

from datetime import date as _date, datetime as _datetime

from rich.console import Console
from rich.prompt import Prompt

from beancount_importer.categorizer.screen import ask_hotkey, bottom_rule, hotkey
from beancount_importer.rules.tags import ActiveTag, RememberedTag, TagStateDelta


# Mode hotkeys for step 2. Numbered for muscle-memory consistency with the
# other positional lists (Screen 2/4); here they unambiguously mean "mode".
_MODE_HOTKEYS: tuple[str, ...] = ("1", "2", "3")

# Step-1 control keys.
_NEW_KEY = "n"
_CLEAR_KEY = "c"
_CANCEL_KEY = "."

# Letters key the known-tag picker. The control keys are excluded so a known
# tag can never shadow `[n]`/`[c]` on the menu — with `c`/`n` reserved, the
# 3rd/14th tag simply skips to the next free letter.
_PICKER_LETTERS = "".join(
    c for c in "abcdefghijklmnopqrstuvwxyz" if c not in (_NEW_KEY, _CLEAR_KEY)
)
_MAX_PICKER = len(_PICKER_LETTERS)


def render(
    console: Console,
    current: ActiveTag | None,
    picker: tuple[RememberedTag, ...] = (),
) -> None:
    """Render step 1 (pick a tag). Pure I/O."""
    console.print()
    line = "  [bold]Tag menu[/] — [dim]pick a tag[/]"
    if current is not None:
        line += f"   active: [magenta]{current.tag}[/] [dim](mode={current.mode})[/]"
    console.print(line)
    if picker:
        chips = "   ".join(
            f"{hotkey(letter)} {rt.tag}"
            for letter, rt in zip(_PICKER_LETTERS, picker, strict=False)
        )
        console.print(f"    {chips}")
    console.print(
        f"    {hotkey(_NEW_KEY)} new tag    "
        f"{hotkey(_CLEAR_KEY)} clear    "
        f"{hotkey(_CANCEL_KEY)} cancel"
    )
    bottom_rule(console)


def run(
    console: Console,
    current: ActiveTag | None,
    known: tuple[RememberedTag, ...] = (),
) -> TagStateDelta | None:
    """Render → prompt → return a delta (or None for cancel/no-change).

    `None` is distinct from `TagStateDelta(op="noop")`: returning None
    signals "the user backed out, do not record any change".

    Single keypress on a real terminal (Ctrl-L redraws); piped input / tests
    fall back to `Prompt.ask` inside `ask_hotkey`. Enter is not a choice — the
    menu has no obvious default, so a stray Enter is swallowed.
    """
    picker = tuple(known)[:_MAX_PICKER]
    render(console, current, picker)
    letters = _PICKER_LETTERS[: len(picker)]
    key = ask_hotkey(
        (*letters, _NEW_KEY, _CLEAR_KEY, _CANCEL_KEY),
        console=console,
        redraw=lambda: render(console, current, picker),
    )
    if key == _CANCEL_KEY:
        return None
    if key == _CLEAR_KEY:
        return TagStateDelta(op="clear")
    if key == _NEW_KEY:
        name = _prompt_tag_name()
        if not name:
            return None
        return _build(console, name, _prompt_mode(console, name), default_until=None)
    # Known-tag pick: name is set; carry any remembered window into the date step.
    chosen = picker[letters.index(key)]
    return _build(console, chosen.tag, _prompt_mode(console, chosen.tag), chosen.until_date)


def _render_mode(console: Console, name: str) -> None:
    """Render step 2 (pick a mode) for the chosen tag name. Pure I/O."""
    console.print()
    console.print(f"  [bold]Apply[/]  [magenta]{name}[/]")
    console.print(
        f"    {hotkey('1')} once   {hotkey('2')} always   {hotkey('3')} until"
    )
    bottom_rule(console)


def _prompt_mode(console: Console, name: str) -> str:
    """Step 2: single-key mode pick for an already-chosen tag name."""
    _render_mode(console, name)
    return ask_hotkey(
        _MODE_HOTKEYS,
        console=console,
        redraw=lambda: _render_mode(console, name),
    )


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


def _prompt_tag_name() -> str:
    """Free-text tag name. Empty input cancels."""
    return Prompt.ask("tag name [dim](empty cancels)[/]", default="").strip()


def _prompt_until_date(console: Console, default: _date | None) -> _date | None:
    """ISO-format date prompt, defaulting to a remembered window when re-picking.

    Accepts `YYYY-MM-DD`. Anything else (including empty with no default)
    returns None so the user can back out without committing a bad date.
    """
    raw = Prompt.ask(
        "Until date [dim](YYYY-MM-DD, empty cancels)[/]",
        default=default.isoformat() if default else "",
    ).strip()
    if not raw:
        return None
    try:
        return _datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        console.print(f"  [yellow]invalid date {raw!r} — cancelling[/]")
        return None
