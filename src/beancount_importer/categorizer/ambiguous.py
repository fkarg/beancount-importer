"""Screen 4 — Ambiguous match selection.

Fires when more than one existing ledger entry scores above the match
threshold. The reference implementation auto-confirms when the top
candidate scores ≥ 1.2 with only trivial diffs; this screen handles
the cases where that auto-confirmation would be unsafe.

Score is rendered as a 5-bar visualization rather than a float — the
user reads "high/medium/low confidence" instantly without translating
the number. Bars assume scores are roughly bounded by 2.0 (the typical
"perfect" upper bound from `matching.scorer`).

Hotkey set:
- `[enter]` pick #1 (highest scored)
- `[1-N]` positional pick by number (capped at 9)
- `[i]` import as new entry
- `[s]` skip   `[q]` quit
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
from beancount_importer.models import LedgerEntry, SourceTransaction


# Score upper bound used for bar normalisation. Scorer outputs cluster
# below ~1.5; capping at 2.0 keeps the bar from saturating on the
# common case while leaving headroom for rule-bonus stacking.
_BAR_FULL = 2.0
_BAR_SEGMENTS = 5
_BAR_FILLED = "▰"
_BAR_EMPTY = "▱"

# Maximum candidates we'll display. Numeric hotkeys cap at 9 (single-key
# entry on `Prompt.ask`); listing more would require multi-digit input
# the design doc rules out.
_MAX_CANDIDATES = 9


@dataclass(frozen=True)
class AmbiguousContext:
    """Inputs for one Screen-4 invocation."""

    txn: SourceTransaction
    candidates: tuple[tuple[LedgerEntry, float], ...]  # already sorted high → low
    progress: tuple[int, int] = (0, 0)
    bank_key: str = ""
    year: int = 0
    active_tag: str | None = None
    tag_remaining: int | None = None


@dataclass(frozen=True)
class AmbiguousDecision:
    """Returned from `run`. Caller routes:

    - `pick`        → use `entry` as the matched ledger entry; transition
                      to Screen 3 if the proposal differs from it
    - `import_new`  → ignore all candidates; create a fresh entry
                      (caller routes through Screen 1/2 to categorize it)
    - `skip`        → no-op for this run
    - `quit`        → tear down
    """

    action: Literal["pick", "import_new", "skip", "quit"]
    entry: LedgerEntry | None = None


# ── Render ────────────────────────────────────────────────────────────────────


def _bars(score: float) -> str:
    """Render `score` (0..~2) as a 5-segment bar.

    Scores >= `_BAR_FULL` saturate to all-filled; scores <= 0 are empty.
    Tests assert on the resulting string so the visualisation can't drift
    silently if the threshold changes.
    """
    if score <= 0:
        filled = 0
    elif score >= _BAR_FULL:
        filled = _BAR_SEGMENTS
    else:
        # Round half up rather than Python's banker's rounding so a
        # score of exactly 1.0 fills 3 bars (the visual "medium"), not 2.
        filled = int((score / _BAR_FULL) * _BAR_SEGMENTS + 0.5)
    return _BAR_FILLED * filled + _BAR_EMPTY * (_BAR_SEGMENTS - filled)


def render(console: Console, ctx: AmbiguousContext) -> None:
    """Append one ambiguous-match block. Pure I/O."""
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
    payee_or_desc = ctx.txn.payee or ctx.txn.description or ""
    console.print(
        f"  [bold]{ctx.txn.booking_date.isoformat()}[/]   "
        f"[bold]{sign}{ctx.txn.amount} {ctx.txn.currency}[/]   "
        f"{payee_or_desc}"
    )
    console.print()
    console.print("  Multiple ledger entries could be this transaction:")
    console.print()
    _render_candidates(console, ctx)
    _render_hotkeys(console)
    bottom_rule(console)


def _render_candidates(console: Console, ctx: AmbiguousContext) -> None:
    """Render the candidate list with score bars + per-entry summary."""
    visible = ctx.candidates[:_MAX_CANDIDATES]
    for i, (entry, score) in enumerate(visible, start=1):
        descr = entry.payee or entry.narration or ""
        target = styled_account(entry.target_account) if entry.target_account else "?"
        console.print(
            f"    {hotkey(str(i))}  [cyan]{_bars(score)}[/]  "
            f"{entry.date.isoformat()}  "
            f'"[dim]{descr}[/]"  '
            f"{target}  {entry.amount}"
        )
    console.print()


def _render_hotkeys(console: Console) -> None:
    """Hotkey row. `[enter]` is `pick #1` because that's the highest scored."""
    console.print(
        f"  {hotkey('enter')} pick #1   {hotkey('1-N')} pick by number"
    )
    console.print(
        f"  {hotkey('i')} import as new entry   "
        f"{hotkey('s')} skip       {hotkey('q')} quit"
    )


# ── Run loop ──────────────────────────────────────────────────────────────────


def _build_hotkeys(ctx: AmbiguousContext) -> tuple[str, ...]:
    n = min(len(ctx.candidates), _MAX_CANDIDATES)
    digits = tuple(str(i) for i in range(1, n + 1))
    # Empty string = Enter = pick #1 (only valid when there's at least one).
    base = ("",) if n > 0 else ()
    return base + digits + ("i", "s", "q")


def run(console: Console, ctx: AmbiguousContext) -> AmbiguousDecision:
    """Render once → prompt → return a typed decision.

    Enter and `[1]` are equivalent (both pick the top candidate); the
    user picks whichever feels natural.
    """
    render(console, ctx)
    key = ask_hotkey(_build_hotkeys(ctx))
    if key == "" or key == "1":
        return AmbiguousDecision(action="pick", entry=ctx.candidates[0][0])
    if key.isdigit():
        return AmbiguousDecision(
            action="pick", entry=ctx.candidates[int(key) - 1][0]
        )
    if key == "i":
        return AmbiguousDecision(action="import_new")
    if key == "s":
        return AmbiguousDecision(action="skip")
    return AmbiguousDecision(action="quit")
