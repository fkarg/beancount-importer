"""Screen 1 — Confirm proposal.

Three entry contexts share one screen:
- `auto_matched`: a categorization rule fired
- `top_candidate`: an existing ledger entry's target was reused
- `fresh_pick`: the user just picked a category on Screen 2

The hotkey row adapts to the context (`[u] fix rule` only when a rule
matched, `💡 N similar upcoming` only on `fresh_pick`), but the body
shape is identical: header → headline → optional provenance → raw
fields → "Will write" block → hotkey row.

Edits append a fresh proposal block; the screen never clears. Tests
capture `console.export_text()` and assert structural elements.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rich.console import Console
from rich.prompt import Prompt

from beancount_importer.categorizer.header import HeaderContext, render_header
from beancount_importer.categorizer.screen import (
    ask_hotkey,
    bottom_rule,
    hotkey,
    styled_account,
)
from beancount_importer.categorizer.modes.amortize import run as run_amortize
from beancount_importer.categorizer.tag_menu import run as run_tag_menu
from beancount_importer.models import (
    CategoryProposal,
    LedgerEntry,
    SourceTransaction,
)
from beancount_importer.rules.models import CategorizationRule
from beancount_importer.rules.tags import ActiveTag


@dataclass(frozen=True)
class ConfirmContext:
    """Inputs for one Screen-1 invocation.

    `bank_account` is the source-side account (e.g. `Assets:B:SPK`) used
    to render the headline arrow. `kind` selects the provenance line.
    `similar_upcoming` populates the `💡` hint on `fresh_pick`; ignored
    on the other kinds.
    """

    txn: SourceTransaction
    proposal: CategoryProposal
    bank_account: str
    kind: Literal["auto_matched", "top_candidate", "fresh_pick"]
    matched_rule: CategorizationRule | None = None
    matched_entry: LedgerEntry | None = None
    progress: tuple[int, int] = (0, 0)
    bank_key: str = ""
    year: int = 0
    active_tag: str | None = None
    tag_remaining: int | None = None
    # Full ActiveTag (mode/until) for the [t] sub-menu's "active" header;
    # the string variant above drives the per-screen state header only.
    current_active_tag: ActiveTag | None = None
    similar_upcoming: int = 0


@dataclass(frozen=True)
class ConfirmDecision:
    """Returned from `run`. The categorizer consumes `proposal` for
    `confirm`; for navigation actions (`change_account`, `open_*`) the
    caller transitions to the appropriate screen.

    Only one transition action ships in Step 2 — the rest land in the
    later steps that build their target screens.
    """

    action: Literal["confirm", "skip", "quit"]
    proposal: CategoryProposal | None = None


# ── Render ────────────────────────────────────────────────────────────────────


def _format_amount(txn: SourceTransaction) -> str:
    sign = "+" if txn.amount > 0 else ""
    return f"{sign}{txn.amount} {txn.currency}"


def render(console: Console, ctx: ConfirmContext) -> None:
    """Append one proposal block to the console output. Pure I/O."""
    header = HeaderContext(
        progress=ctx.progress,
        bank_key=ctx.bank_key,
        year=ctx.year,
        active_tag=ctx.active_tag,
        tag_remaining=ctx.tag_remaining,
        glyph="✎",
    )
    render_header(console, header)

    # When the proposal has no target (degenerate state — empty postings),
    # show a bare `?` rather than gluing the neutral glyph onto a literal
    # "?" string. The user reads this as "still missing", not "neutral class".
    target_str = (
        styled_account(ctx.proposal.target_account)
        if ctx.proposal.target_account
        else "?"
    )
    headline = (
        f"  [bold]{ctx.txn.booking_date.isoformat()}[/]   "
        f"[bold]{_format_amount(ctx.txn)}[/]   "
        f"{styled_account(ctx.bank_account)}  →  {target_str}"
    )
    console.print(headline)
    console.print()

    _render_provenance(console, ctx)
    _render_raw_block(console, ctx)
    _render_will_write_block(console, ctx)
    _render_hotkeys(console, ctx)
    bottom_rule(console)


def _render_provenance(console: Console, ctx: ConfirmContext) -> None:
    if ctx.kind == "auto_matched" and ctx.matched_rule is not None:
        rule = ctx.matched_rule
        pattern = rule.payee_pattern or rule.description_pattern or ""
        field = "payee" if rule.payee_pattern else "description"
        console.print(
            f"  Matched rule: [cyan]/{pattern}/[/]  (in {field})"
        )
        console.print()
    elif ctx.kind == "top_candidate" and ctx.matched_entry is not None:
        console.print(
            f"  Reusing target from existing entry on "
            f"[dim]{ctx.matched_entry.date.isoformat()}[/]"
        )
        console.print()
    elif ctx.kind == "fresh_pick" and ctx.similar_upcoming > 0:
        console.print(
            f"  💡 [cyan]{ctx.similar_upcoming}[/] more similar transactions upcoming"
        )
        console.print()


def _render_raw_block(console: Console, ctx: ConfirmContext) -> None:
    payee = ctx.txn.payee or "—"
    description = ctx.txn.description or "—"
    console.print("  Raw:")
    console.print(f"    payee:        [dim]{payee}[/]")
    console.print(f"    description:  [dim]{description}[/]")
    console.print()


def _render_will_write_block(console: Console, ctx: ConfirmContext) -> None:
    p = ctx.proposal
    payee_out = p.payee or ctx.txn.payee or ""
    narr_out = p.narration or ctx.txn.description or ""
    target = styled_account(p.target_account) if p.target_account else "?"
    console.print("  Will write:")
    console.print(f'    narration:    "{narr_out}"')
    console.print(f'    payee:        "{payee_out}"')
    console.print(f"    category:     {target}")
    if p.tag:
        console.print(f"    tag:          [magenta]#{p.tag}[/]")
    console.print()


def _render_hotkeys(console: Console, ctx: ConfirmContext) -> None:
    """Hotkey row.

    Steps 10–11 add `[t]` (tag menu) and `[m]` (mode menu — currently
    just amortize). The remaining cross-screen affordances (`[c]`,
    `[r]`, `[u]`) come online as their target screens land.
    """
    is_debit = ctx.txn.amount < 0
    console.print(
        f"  {hotkey('enter')} confirm  "
        f"{hotkey('n')} narration  "
        f"{hotkey('p')} payee"
    )
    # `[m]` only appears for debits — amortizing income makes no sense
    # in any of the three plugin modes, so the menu would only mislead.
    mode_part = f"  {hotkey('m')} amortize" if is_debit else ""
    console.print(
        f"  {hotkey('t')} tag menu{mode_part}  "
        f"{hotkey('s')} skip       "
        f"{hotkey('q')} quit"
    )


# ── Run loop ──────────────────────────────────────────────────────────────────


_HOTKEYS_DEBIT: tuple[str, ...] = ("", "n", "p", "t", "m", "s", "q")
_HOTKEYS_CREDIT: tuple[str, ...] = ("", "n", "p", "t", "s", "q")


def run(console: Console, ctx: ConfirmContext) -> ConfirmDecision:
    """Render → prompt → handle → loop until commit/skip/quit.

    Edits update the proposal in-place (frozen-model `model_copy`) and
    re-render below the previous block. Empty input means Enter.
    """
    from dataclasses import replace

    proposal = ctx.proposal
    keys = _HOTKEYS_DEBIT if ctx.txn.amount < 0 else _HOTKEYS_CREDIT
    while True:
        render(console, replace(ctx, proposal=proposal))
        key = ask_hotkey(keys)
        if key == "":
            return ConfirmDecision(action="confirm", proposal=proposal)
        if key == "s":
            return ConfirmDecision(action="skip")
        if key == "q":
            return ConfirmDecision(action="quit")
        # Edit/menu hotkeys: mutate `proposal` and loop. `Prompt.ask`'s
        # `choices` list keeps `key` inside this set — no fallthrough.
        if key == "n":
            current = proposal.narration or ctx.txn.description or ""
            new = Prompt.ask(f"narration [{current}]", default=current)
            proposal = proposal.model_copy(update={"narration": new})
        elif key == "p":
            current = proposal.payee or ctx.txn.payee or ""
            new = Prompt.ask(f"payee [{current}]", default=current)
            proposal = proposal.model_copy(update={"payee": new})
        elif key == "t":
            delta = run_tag_menu(console, ctx.current_active_tag)
            if delta is not None:
                proposal = proposal.model_copy(update={"tag_state_delta": delta})
        else:  # key == "m" — amortize mode (debits only)
            proposal = run_amortize(console, proposal)
