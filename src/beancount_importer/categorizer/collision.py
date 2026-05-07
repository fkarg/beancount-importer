"""Screen 3 — Existing-entry collision (merge prompt).

The pipeline computes `proposed_changes` between an existing ledger entry
and a new CSV-driven proposal. Currently those changes silently apply
(after Phase 0); this screen makes them actionable: update / keep / import
new / block future updates / skip / quit.

Empty `proposed_changes` short-circuit before this screen — they're
silent skips with a `…` ticker line, never user-visible. Only renders
when there's a real diff to resolve.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rich.console import Console

from beancount_importer.categorizer.header import HeaderContext, render_header
from beancount_importer.categorizer.screen import (
    ask_hotkey,
    bottom_rule,
    hotkey,
    styled_account,
)
from beancount_importer.models import (
    CategoryProposal,
    LedgerEntry,
    ProposedChange,
    SourceTransaction,
)


@dataclass(frozen=True)
class CollisionContext:
    """Inputs for one Screen-3 invocation."""

    txn: SourceTransaction
    existing: LedgerEntry
    proposed_changes: list[ProposedChange]
    proposal: CategoryProposal  # what would be written on `update`
    progress: tuple[int, int] = (0, 0)
    bank_key: str = ""
    year: int = 0
    active_tag: str | None = None
    tag_remaining: int | None = None


@dataclass(frozen=True)
class CollisionDecision:
    """Returned from `run`. The caller routes based on `action`:

    - `update`  → splice the matched entry with `proposal`
    - `keep`    → leave existing alone, do not re-import
    - `import_new` → create a new entry (caller may re-run categorize)
    - `block`   → install skip-update rule for this payee, drop the row
    - `skip`    → no-op for this run; row reappears next time
    - `quit`    → tear down the run
    """

    action: Literal["update", "keep", "import_new", "block", "skip", "quit"]


# ── Render ────────────────────────────────────────────────────────────────────


def render(console: Console, ctx: CollisionContext) -> None:
    """Append one collision block. Pure I/O."""
    header = HeaderContext(
        progress=ctx.progress,
        bank_key=ctx.bank_key,
        year=ctx.year,
        active_tag=ctx.active_tag,
        tag_remaining=ctx.tag_remaining,
        glyph="⚡",
    )
    render_header(console, header)

    existing = ctx.existing
    sign = "+" if ctx.txn.amount > 0 else ""
    console.print(
        f"  [bold]{ctx.txn.booking_date.isoformat()}[/]   "
        f"[bold]{sign}{ctx.txn.amount} {ctx.txn.currency}[/]  "
        "already imported as:"
    )
    console.print()
    _render_existing_entry(console, existing)
    console.print()
    _render_diff(console, ctx.proposed_changes)
    _render_hotkeys(console)
    bottom_rule(console)


def _render_existing_entry(console: Console, entry: LedgerEntry) -> None:
    """Render the matched entry as if it were beancount source.

    The textual shape mirrors what the user has in the file, modulo the
    glyph + style on the accounts. The amount sign follows the source
    posting (negative on the bank side for a debit).
    """
    payee_part = f'"{entry.payee}" ' if entry.payee else ""
    console.print(
        f'    {entry.date.isoformat()} {entry.flag} '
        f'{payee_part}"{entry.narration}"'
    )
    console.print(
        f"      {styled_account(entry.source_account)}"
        f"            {entry.amount} {entry.currency}"
    )
    if entry.target_account:
        console.print(f"      {styled_account(entry.target_account)}")


def _render_diff(console: Console, changes: list[ProposedChange]) -> None:
    """Render `field: "old"  →  "new"` lines, old dim red, new green."""
    console.print("  Proposed changes from CSV:")
    for change in changes:
        console.print(
            f'    {change.field}:        '
            f'[red dim]"{change.old_val}"[/]  →  '
            f'[green]"{change.new_val}"[/]'
        )
    console.print()


def _render_hotkeys(console: Console) -> None:
    """Hotkey row for Step 2's slice. `[i] import_new` defers to a later
    step (it routes back through Screen 1/2 which haven't shipped yet) —
    same for `[c] change account`. Both letters stay reserved.
    """
    console.print(
        f"  {hotkey('enter')} update   "
        f"{hotkey('k')} keep existing   "
        f"{hotkey('b')} block future updates"
    )
    console.print(
        f"                  {hotkey('s')} skip       {hotkey('q')} quit"
    )


# ── Run loop ──────────────────────────────────────────────────────────────────


_HOTKEYS: tuple[str, ...] = ("", "k", "b", "s", "q")


def run(console: Console, ctx: CollisionContext) -> CollisionDecision:
    """Render once → prompt → return decision. No edit affordances on
    this screen; the diff itself is the decision aid.
    """
    render(console, ctx)
    key = ask_hotkey(_HOTKEYS)
    if key == "":
        return CollisionDecision(action="update")
    if key == "k":
        return CollisionDecision(action="keep")
    if key == "b":
        return CollisionDecision(action="block")
    if key == "s":
        return CollisionDecision(action="skip")
    return CollisionDecision(action="quit")
