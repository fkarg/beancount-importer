"""Per-result ticker — one line per finalised `ImportResult`.

Replaces `RichReporter.on_result`'s no-op so the user sees what's
happening as the run progresses, instead of only at the final summary.

Format (columns separated by two spaces; account-glyph + name on the
right where applicable):

```
✓  2024-03-04  -12.50 EUR  Starbucks         → ↓ Expenses:Food          (rule)
↻  2024-03-07  +5.00 EUR   Refund            → ↑ Income:Refunds         (replay)
…  2024-03-08  -12.50 EUR  Starbucks                                  matched
✎  2024-03-09  -15.00 EUR  Spotify           → ↓ Expenses:Subscriptions (updated)
```

Glyph legend:
- `✓` written cleanly (rule-driven or user-confirmed)
- `↻` replayed from decision log
- `…` matched (already in the ledger — no action needed) or skipped
  (user-installed pattern or Screen 3 decision)
- `✎` updated existing entry
- `⚠` needs follow-up

Pure function: `format_line(result) -> str`. The caller chooses where
to print it (RichReporter.on_result prints to the global console).
"""

from __future__ import annotations

from beancount_importer.categorizer.screen import styled_account
from beancount_importer.models import ImportResult


# Two-space columns; description column trimmed to keep the line
# scannable. Wider terminals get extra trailing whitespace, narrower
# ones wrap — Rich handles both gracefully.
_DESCRIPTION_WIDTH = 18


def _amount(result: ImportResult) -> str:
    txn = result.source_txn
    sign = "+" if txn.amount > 0 else ""
    return f"{sign}{txn.amount} {txn.currency}"


def _description(result: ImportResult) -> str:
    """Pick the most informative short label for the row.

    Precedence: proposal payee > txn payee > txn description. Truncates
    so wider payees don't push the target column off-screen on narrow
    terminals.
    """
    p = result.proposal
    txn = result.source_txn
    label = (p.payee if p and p.payee else None) or txn.payee or txn.description or ""
    if len(label) > _DESCRIPTION_WIDTH:
        label = label[: _DESCRIPTION_WIDTH - 1] + "…"
    return label.ljust(_DESCRIPTION_WIDTH)


def _target_chunk(result: ImportResult) -> str:
    """`→ ↓ Expenses:Food` style chunk, or empty for skip-shaped results."""
    p = result.proposal
    if p is None or not p.target_account:
        return ""
    return f"→ {styled_account(p.target_account)}"


def _classify(result: ImportResult) -> tuple[str, str]:
    """Return `(glyph, suffix)` for the result.

    The glyph leads the line; the suffix labels the path taken
    ((rule), (you), (replay), reason text). Suffixes are deliberately
    short — the user is scanning, not reading prose.

    The three internal "matched-via-different-mechanism" outcomes —
    cheap dedup, cross-source matcher, and seed-silent-skip — all
    collapse to one user-facing `matched` label. The mechanism only
    surfaces as a parenthetical when it carries useful information
    (cross-source match → the row's counterpart lives on another bank).
    """
    if result.is_replay:
        return ("↻", "(replay)")
    if result.action == "skip":
        reason = result.skip_reason
        # Three different internal mechanisms; one user-facing concept.
        if reason in ("duplicate", "cross_source_match"):
            suffix = "matched"
            if reason == "cross_source_match":
                suffix = "matched (other bank)"
            return ("…", suffix)
        # Suppression paths — the row would have been actionable but
        # something explicit told the pipeline to leave it alone.
        if reason == "skip_rule":
            return ("…", "skipped (rule)")
        if reason == "user_skipped":
            return ("…", "skipped (you)")
        if reason == "user_blocked":
            return ("…", "blocked (rule installed)")
        return ("…", reason or "skipped")
    if result.action == "update":
        if not result.proposed_changes:
            # Silent shortcut — either the seed matched the entry verbatim
            # or the user confirmed via Screen 3's [k] keep.
            if result.skip_reason == "user_kept":
                return ("…", "matched (you confirmed)")
            return ("…", "matched")
        return ("✎", "(updated)")
    if result.action == "new":
        if result.rule_matched is not None:
            return ("✓", "(rule)")
        return ("✓", "(you)")
    if result.action == "quit":
        return ("⚠", "(quit)")
    # `transfer` and any other future action: still mark it written.
    return ("✓", f"({result.action})")


def format_line(result: ImportResult) -> str:
    """Render one ticker line as a Rich-markup string.

    The line uses the same glyph + style grammar the screens do, so the
    visual rhythm is consistent between active screens and the scrollback.
    """
    glyph, suffix = _classify(result)
    txn_date = result.source_txn.booking_date.isoformat()
    target = _target_chunk(result)
    if target:
        return f"{glyph}  {txn_date}  {_amount(result):>10}  {_description(result)}  {target}  [dim]{suffix}[/]"
    return f"{glyph}  {txn_date}  {_amount(result):>10}  {_description(result)}  [dim]{suffix}[/]"
