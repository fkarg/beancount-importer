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
    tag_remaining_days,
)
from beancount_importer.categorizer.modes.amortize import run as run_amortize
from beancount_importer.categorizer.rule_editor import run as run_rule_editor
from beancount_importer.categorizer.tag_menu import run as run_tag_menu
from beancount_importer.models import (
    CategoryProposal,
    LedgerEntry,
    SourceTransaction,
)
from beancount_importer.rules.models import CategorizationRule
from beancount_importer.rules.tags import ActiveTag, RememberedTag

# Avoid an import cycle by typing through a TYPE_CHECKING shim — `pipeline`
# imports from `categorizer/host` which imports this module.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from beancount_importer.pipeline import NearMiss


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
    # Known tags offered by the [t] sub-menu's picker (recent + ledger,
    # grown in-session). Ordered most-relevant-first.
    known_tags: tuple[RememberedTag, ...] = ()
    similar_upcoming: int = 0
    # Diagnostic-only: rendered above the hotkey row when no entry was
    # matched. Surfaces *why* the user is being prompted instead of seeing
    # a silent skip — usually a sub-account placement or rule-cleaned text
    # that drifted under `min_score`.
    near_misses: tuple[NearMiss, ...] = ()


@dataclass(frozen=True)
class ConfirmDecision:
    """Returned from `run`. The categorizer consumes `proposal` for
    `confirm`; for navigation actions (`change_account`, `open_*`) the
    caller transitions to the appropriate screen and is responsible for
    looping back here with an updated context.

    `change_account` carries the in-flight `proposal` so the host can
    preserve the user's narration/payee/tag edits across the round-trip
    to Screen 2.
    """

    action: Literal["confirm", "skip", "quit", "change_account"]
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
    render_near_misses(console, ctx.near_misses, ctx.bank_account)
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


def _effective_tag(ctx: ConfirmContext) -> str | None:
    """The tag this txn will actually be written with — what the preview shows.

    Mirrors the pipeline's `_stamp_active_tag`: an already-stamped
    `proposal.tag` wins; otherwise the active tag applies iff its window
    includes the txn's booking date. Without this the `tag:` line lagged
    the real state — the pipeline stamps `proposal.tag` only *after* this
    screen returns, so an active duration/always tag was invisible here.
    """
    if ctx.proposal.tag:
        return ctx.proposal.tag
    tag = ctx.current_active_tag
    if tag is not None and tag.applies_to(ctx.txn.booking_date):
        return tag.tag
    return None


def _render_will_write_block(console: Console, ctx: ConfirmContext) -> None:
    """Render the "Will write" block with per-field diff highlighting.

    When `ctx.matched_entry` is set (rule fired against an existing
    entry, or a top-candidate match), the rendering diffs each field
    against the entry: unchanged fields are dim, changed fields are
    yellow with `(was: "old")`. Fresh imports (no matched_entry) show
    every field plain — the whole row IS new.
    """
    p = ctx.proposal
    entry = ctx.matched_entry
    payee_out = p.payee or ctx.txn.payee or ""
    narr_out = p.narration or ctx.txn.description or ""

    console.print("  Will write:")
    _render_will_write_field(
        console, "narration:", f'"{narr_out}"',
        new_value=narr_out,
        old_value=entry.narration if entry else None,
    )
    _render_will_write_field(
        console, "payee:", f'"{payee_out}"',
        new_value=payee_out,
        old_value=entry.payee if entry else None,
    )
    target_str = styled_account(p.target_account) if p.target_account else "?"
    old_target = entry.target_account if entry else None
    old_target_styled = (
        styled_account(old_target) if old_target else None
    )
    _render_will_write_field(
        console, "category:", target_str,
        new_value=p.target_account or "",
        old_value=old_target,
        old_rendered=old_target_styled,
    )
    if (tag := _effective_tag(ctx)) is not None:
        console.print(f"    tag:          [magenta]#{tag}[/]")
    if p.save_as_rule:
        # Show what the rule will match on, so the user knows what they're
        # locking in. The editor puts its edited rule on `pending_rule`; when
        # absent (replay / non-interactive save) fall back to the derive
        # heuristic (payee wins, description falls back).
        if p.pending_rule is not None:
            pr = p.pending_rule
            rule_field = "payee" if pr.payee_pattern else "description"
            rule_value = pr.payee_pattern or pr.description_pattern
            console.print(
                f"    save as rule: [green]✓[/] "
                f"[dim](on {rule_field} {pr.match_mode}: {rule_value!r})[/]"
            )
        else:
            rule_field = "payee" if ctx.txn.payee else "description"
            rule_value = ctx.txn.payee or ctx.txn.description or ""
            console.print(
                f"    save as rule: [green]✓[/] "
                f"[dim](on {rule_field}: {rule_value!r})[/]"
            )
    console.print()


def _render_will_write_field(
    console: Console,
    label: str,
    rendered: str,
    *,
    new_value: str,
    old_value: str | None,
    old_rendered: str | None = None,
) -> None:
    """One line of the "Will write" block, with diff styling.

    `old_value=None` means "no comparison entry" → render plain
    (a fresh import). Equal values render dim ("nothing to do here").
    A real change renders yellow with the old text appended dim.

    `old_rendered` is the display form of `old_value` for the suffix.
    Defaults to the quoted text form (right for narration/payee);
    callers wanting glyph-styled accounts pass a pre-styled string.
    """
    label_padded = f"{label:<14}"
    if old_value is None:
        console.print(f"    {label_padded}{rendered}")
        return
    if new_value == (old_value or ""):
        console.print(f"    [dim]{label_padded}{rendered}[/]")
        return
    if old_value:
        old_display = old_rendered if old_rendered else f'"{old_value}"'
        suffix = f" [dim](was:[/] {old_display}[dim])[/]"
    else:
        suffix = " [dim](new)[/]"
    console.print(f"    {label_padded}[yellow]{rendered}[/]{suffix}")


def render_near_misses(
    console: Console,
    near_misses: tuple,
    bank_account: str,
) -> None:
    """Render at most one diagnostic line per `NearMiss`.

    Shared by Screen 1 (`confirm`) and Screen 2 (`pick`) so the reasons
    surface identically regardless of which prompt the user lands on.
    `bank_account` is the txn's source-side account, used to make the
    `different_bucket` line concretely tell the user *which* bucket the
    closer entry was supposed to be in.
    """
    if not near_misses:
        return
    for miss in near_misses:
        if miss.reason == "below_threshold":
            console.print(
                f"  [yellow]⚠[/]  Closest existing on [dim]{bank_account}[/]: "
                f"{miss.entry.date.isoformat()} "
                f"{miss.entry.amount:+} {miss.entry.currency} "
                f"[dim](score {miss.score:.2f}, below threshold)[/]"
            )
        else:  # different_bucket
            console.print(
                f"  [yellow]⚠[/]  Same amount/date elsewhere: "
                f"[dim]{miss.entry.source_account}[/] "
                f"{miss.entry.date.isoformat()} "
                f"{miss.entry.amount:+} {miss.entry.currency} "
                f"[dim](not in this bank's bucket)[/]"
            )
    console.print()


def _render_hotkeys(console: Console, ctx: ConfirmContext) -> None:
    """Hotkey row.

    `[r]` opens the rule editor (an editable MATCH→WRITE panel); saving it
    stages a `CategorizationRule` on the proposal for persistence on confirm.
    `[u]` (fix an already-matched rule) is still deferred.
    """
    is_debit = ctx.txn.amount < 0
    rule_label = (
        f"{hotkey('r')} [green]✓[/] save as rule"
        if ctx.proposal.save_as_rule
        else f"{hotkey('r')} save as rule"
    )
    console.print(
        f"  {hotkey('enter')} confirm  "
        f"{hotkey('n')} narration  "
        f"{hotkey('p')} payee  "
        f"{hotkey('c')} change account"
    )
    # `[m]` only appears for debits — amortizing income makes no sense
    # in any of the three plugin modes, so the menu would only mislead.
    mode_part = f"  {hotkey('m')} amortize" if is_debit else ""
    console.print(
        f"  {rule_label}  "
        f"{hotkey('t')} tag menu{mode_part}  "
        f"{hotkey('s')} skip  "
        f"{hotkey('q')} quit"
    )


# ── Run loop ──────────────────────────────────────────────────────────────────


_HOTKEYS_DEBIT: tuple[str, ...] = ("", "n", "p", "c", "r", "t", "m", "s", "q")
_HOTKEYS_CREDIT: tuple[str, ...] = ("", "n", "p", "c", "r", "t", "s", "q")


def run(console: Console, ctx: ConfirmContext) -> ConfirmDecision:
    """Render → prompt → handle → loop until commit/skip/quit.

    Edits update the proposal in-place (frozen-model `model_copy`) and
    re-render below the previous block. Empty input means Enter.
    """
    from dataclasses import replace

    proposal = ctx.proposal
    keys = _HOTKEYS_DEBIT if ctx.txn.amount < 0 else _HOTKEYS_CREDIT
    while True:
        display = replace(ctx, proposal=proposal)
        render(console, display)
        key = ask_hotkey(
            keys,
            console=console,
            redraw=lambda display=display: render(console, display),
        )
        if key == "":
            return ConfirmDecision(action="confirm", proposal=proposal)
        if key == "s":
            return ConfirmDecision(action="skip")
        if key == "q":
            return ConfirmDecision(action="quit")
        if key == "c":
            # Hand control to the host — it owns Screen 2 plumbing
            # (suggestions, all_accounts) which Screen 1 doesn't.
            return ConfirmDecision(action="change_account", proposal=proposal)
        if key == "r":
            # Open the rule editor pre-filled from this txn + proposal. On
            # save it returns the edited rule, which rides on the proposal to
            # the pipeline verbatim; cancel leaves the proposal untouched.
            edited = run_rule_editor(console, ctx.txn, proposal)
            if edited is not None:
                proposal = proposal.model_copy(
                    update={"save_as_rule": True, "pending_rule": edited}
                )
            continue
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
            delta = run_tag_menu(console, ctx.current_active_tag, ctx.known_tags)
            if delta is not None:
                proposal = proposal.model_copy(update={"tag_state_delta": delta})
                # Reflect the pending delta in the next render: the header
                # and "Will write" tag line read off `ctx`, and re-opening
                # the menu should show the now-active tag. The pipeline still
                # owns the real stamp; this only keeps the preview honest.
                new_active = delta.new_state if delta.op == "set" else None
                ctx = replace(
                    ctx,
                    current_active_tag=new_active,
                    active_tag=new_active.tag if new_active else None,
                    tag_remaining=tag_remaining_days(
                        new_active, ctx.txn.booking_date
                    ),
                )
        else:  # key == "m" — amortize mode (debits only)
            proposal = run_amortize(console, proposal)
