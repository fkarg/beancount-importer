"""Screen 2 — Pick a category.

The only screen where Enter is unbound. There's no proposal yet to
accept; the user must positively choose. The header glyph is `?` to
match the principle ("color and shape both carry meaning").

Hotkey set:
- `[1-5]` pick from the top-5 suggestions
- `[l]` list all accounts (paged 10/page)
- `[w]` write a custom account name
- `[o]` transfer to own account (filtered to Assets/Liabilities)
- `[s]` skip
- `[q]` quit

`[r]` (rule editor) and `[o]` (transfer-own) ship across multiple steps;
this module includes `[o]` because it composes from primitives this
screen already needs (a filtered numbered list).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from rich.console import Console
from rich.prompt import Prompt

from beancount_importer.categorizer.confirm import render_near_misses
from beancount_importer.categorizer.header import HeaderContext, render_header
from beancount_importer.categorizer.screen import (
    ask_hotkey,
    bottom_rule,
    hotkey,
    styled_account,
)
from beancount_importer.models import LedgerEntry, SourceTransaction

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from beancount_importer.pipeline import NearMiss


# How many suggestions land on the top screen. The design doc settles on 5
# (vs. an earlier draft's 10) because a five-row block keeps the screen
# compact and `[l]` paging covers the long tail.
SUGGESTIONS_TOP_N = 5


@dataclass(frozen=True)
class PickContext:
    """Inputs for one Screen-2 invocation."""

    txn: SourceTransaction
    bank_account: str
    suggestions: tuple[str, ...]               # already ranked by `rank_accounts`
    suggestion_counts: dict[str, int]          # frequency annotations (dim "34×")
    all_accounts: tuple[str, ...]              # full pool for `[l]` paging
    existing_entries: tuple[LedgerEntry, ...]  # for `[o]` transfer-to-own filter
    progress: tuple[int, int] = (0, 0)
    bank_key: str = ""
    year: int = 0
    active_tag: str | None = None
    tag_remaining: int | None = None
    near_misses: tuple[NearMiss, ...] = ()


@dataclass(frozen=True)
class PickDecision:
    """Returned from `run`. The caller transitions to Screen 1 with
    `account` pre-filled when `action == "pick"`.
    """

    action: Literal["pick", "skip", "quit"]
    account: str | None = None


# ── Render ────────────────────────────────────────────────────────────────────


def render(console: Console, ctx: PickContext) -> None:
    """Append one pick block to the console output. Pure I/O."""
    header = HeaderContext(
        progress=ctx.progress,
        bank_key=ctx.bank_key,
        year=ctx.year,
        active_tag=ctx.active_tag,
        tag_remaining=ctx.tag_remaining,
        glyph="?",
    )
    render_header(console, header)

    sign = "+" if ctx.txn.amount > 0 else ""
    console.print(
        f"  [bold]{ctx.txn.booking_date.isoformat()}[/]   "
        f"[bold]{sign}{ctx.txn.amount} {ctx.txn.currency}[/]   "
        f"{styled_account(ctx.bank_account)}  →  ?"
    )
    console.print()

    payee = ctx.txn.payee or "—"
    description = ctx.txn.description or "—"
    console.print("  Raw:")
    console.print(f"    payee:        [dim]{payee}[/]")
    console.print(f"    description:  [dim]{description}[/]")
    console.print()

    _render_suggestions(console, ctx)
    render_near_misses(console, ctx.near_misses, ctx.bank_account)
    _render_hotkeys(console)
    bottom_rule(console)


def _render_suggestions(console: Console, ctx: PickContext) -> None:
    """Render the top-N suggestions list with usage counts."""
    suggestions = ctx.suggestions[:SUGGESTIONS_TOP_N]
    if not suggestions:
        console.print("  [dim]No suggestions — use [w] to write a custom account.[/]")
        console.print()
        return
    console.print("  Suggestions (top by use):")
    for i, account in enumerate(suggestions, start=1):
        count = ctx.suggestion_counts.get(account)
        count_str = f"{count}×" if count else "—"
        console.print(
            f"    {hotkey(str(i))} {styled_account(account):<40}   "
            f"[dim]{count_str}[/]"
        )
    console.print()


def _render_hotkeys(console: Console) -> None:
    """Hotkey row. Step 4 ships `[1-5]`, `[l]`, `[w]`, `[o]`, `[s]`, `[q]`."""
    console.print(
        f"  {hotkey('1-5')} pick   "
        f"{hotkey('l')} list all accounts   "
        f"{hotkey('w')} write custom account"
    )
    console.print(
        f"  {hotkey('o')} transfer to own account   "
        f"{hotkey('s')} skip       {hotkey('q')} quit"
    )


# ── Run loop ──────────────────────────────────────────────────────────────────


def _build_hotkeys(ctx: PickContext) -> tuple[str, ...]:
    """Numeric keys are gated on the actual suggestion count.

    A user pressing `5` when only three suggestions render is a typo;
    `Prompt.ask`'s choice list rejects it before it reaches the run loop.
    """
    n = min(len(ctx.suggestions), SUGGESTIONS_TOP_N)
    digits = tuple(str(i) for i in range(1, n + 1))
    return digits + ("l", "w", "o", "s", "q")


def run(console: Console, ctx: PickContext) -> PickDecision:
    """Render → prompt → handle. Each path either picks an account or
    transitions to a sub-prompt that does.

    Re-renders Screen 2 after every sub-prompt cancel so the user's
    landing view is always the hotkey row, never the tail of a list
    they just backed out of.
    """
    while True:
        render(console, ctx)
        key = ask_hotkey(_build_hotkeys(ctx))
        if key == "s":
            return PickDecision(action="skip")
        if key == "q":
            return PickDecision(action="quit")
        if key.isdigit():
            return PickDecision(
                action="pick", account=ctx.suggestions[int(key) - 1]
            )
        if key == "l":
            picked = _full_list(console, ctx.all_accounts)
            if picked is not None:
                return PickDecision(action="pick", account=picked)
            continue
        if key == "w":
            picked = _write_custom(console, ctx.all_accounts)
            if picked is not None:
                return PickDecision(action="pick", account=picked)
            continue
        # key == "o"
        picked = _transfer_to_own(console, ctx.existing_entries)
        if picked is not None:
            return PickDecision(action="pick", account=picked)


# ── Sub-prompts ───────────────────────────────────────────────────────────────


def _full_list(console: Console, accounts: Iterable[str]) -> str | None:
    """Show every `accounts` entry at once in an alphabetical column grid.

    Earlier this was 10-per-page paging, but the typical user has ~100
    accounts and wants to *scan* visually for the one they need —
    paging makes that much harder than it has to be. Terminal scroll
    handles overflow if the list is huge.

    Input is free-text rather than `Prompt.ask(choices=…)` because
    indices may be three digits (>9 accounts blows the single-key cap).
    `[x]` cancels, `[enter]` redraws (so the user can refresh after a
    typo warning), any positive integer in range picks that entry.
    """
    items = sorted(accounts)
    if not items:
        console.print("  [dim](no accounts loaded)[/]")
        return None
    while True:
        _render_column_grid(console, items)
        console.print(
            f"  {hotkey('1-' + str(len(items)))} pick   "
            f"{hotkey('enter')} redraw   {hotkey('x')} cancel"
        )
        raw = Prompt.ask(
            ">", default="", show_default=False
        ).strip()
        if raw == "x":
            return None
        if raw == "":
            continue
        try:
            idx = int(raw)
        except ValueError:
            console.print(f"  [yellow]not a number: {raw!r}[/]")
            continue
        if not 1 <= idx <= len(items):
            console.print(
                f"  [yellow]index {idx} out of range (1-{len(items)})[/]"
            )
            continue
        return items[idx - 1]


def _render_column_grid(console: Console, items: list[str]) -> None:
    """Lay `items` out column-major so reading top-to-bottom is alphabetical.

    Column count adapts to terminal width with the longest account name
    setting cell width. Column-major (vs. row-major) means columns 1, 2,
    3 contain items 1..r, r+1..2r, 2r+1..3r — so a user scanning a
    sorted list moves their eyes down a column, not zig-zag across rows.
    """
    n_digits = len(str(len(items)))
    max_acc_len = max(len(a) for a in items)
    # `[NN] ◆ ` = n_digits + 6 chars overhead; trailing pad keeps cells
    # from running into each other.
    cell_width = n_digits + 6 + max_acc_len + 2
    n_cols = max(1, (console.width - 2) // cell_width)
    n_rows = (len(items) + n_cols - 1) // n_cols
    for row in range(n_rows):
        cells = []
        for col in range(n_cols):
            idx = col * n_rows + row
            if idx >= len(items):
                continue
            label = f"[cyan]\\[{idx + 1:>{n_digits}}][/]"
            cells.append(
                f"{label} {styled_account(items[idx]):<{max_acc_len}}"
            )
        console.print("  " + "  ".join(cells))
    console.print()


def _write_custom(console: Console, known: tuple[str, ...]) -> str | None:
    """Free-text account entry. Names not in `known` ask for confirmation
    so a typo doesn't silently land an unfamiliar account in the ledger.
    """
    name = Prompt.ask("account name", default="").strip()
    if not name:
        return None
    if name in known:
        return name
    console.print(
        f"  [yellow]'{name}' is not in any existing entry yet.[/]"
    )
    confirm = Prompt.ask(
        "use anyway?", choices=["y", "n"], default="n", show_choices=False
    ).strip()
    if confirm == "y":
        return name
    return None


def _transfer_to_own(
    console: Console, entries: tuple[LedgerEntry, ...]
) -> str | None:
    """List Assets/Liabilities accounts (any side of any entry) and pick one.

    Reuses `_full_list`'s shape — same column grid, narrower input pool.
    """
    seen: dict[str, None] = {}
    for entry in entries:
        for acc in (entry.source_account, entry.target_account):
            if acc and acc.startswith(("Assets:", "Liabilities:")):
                seen.setdefault(acc, None)
    if not seen:
        console.print("  [dim](no own-account history yet)[/]")
        return None
    return _full_list(console, sorted(seen))
