"""Suggest target accounts for a new transaction, ordered by relevance.

Pure function with three composable signals:

1. Candidate ledger entries (already-scored matches) — their target accounts
   bubble to the top because the user has classified similar transactions
   the same way before.
2. Frequency in the loaded ledger — accounts the user reaches for often
   are good defaults.
3. Sign bias — debits (`amount < 0`) prefer `Expenses:`/`Liabilities:`;
   credits prefer `Income:`/`Assets:`. Filters out the obvious wrong half
   so the top of the list is genuinely actionable.

Returns `(top_accounts, all_accounts)` so the caller can render a short
numbered list and still accept any account from the longer pool.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from beancount_importer.models import LedgerEntry, SourceTransaction


def rank_accounts(
    txn: SourceTransaction,
    candidates: Iterable[tuple[LedgerEntry, float]],
    all_existing: Iterable[LedgerEntry],
    *,
    top_n: int = 10,
    suggested_target: str | None = None,
) -> tuple[list[str], list[str]]:
    """Return `(top_n_suggestions, all_known_accounts)`.

    `suggested_target` (e.g. a rule's target) is always at index 0 of the
    suggestions list — the user expects their rule's choice to win.
    """
    is_debit = txn.amount < 0
    candidate_accounts = {entry.target_account for entry, _ in candidates if entry.target_account}
    counter: Counter[str] = Counter()
    for entry in all_existing:
        if entry.source_account:
            counter[entry.source_account] += 1
        if entry.target_account:
            counter[entry.target_account] += 1
    all_accounts = sorted(counter)

    def _sign_score(account: str) -> int:
        if is_debit:
            if account.startswith(("Expenses:", "Liabilities:")):
                return 2
            if account.startswith(("Assets:", "Income:")):
                return 0
        else:
            if account.startswith(("Income:", "Assets:")):
                return 2
            if account.startswith(("Expenses:", "Liabilities:")):
                return 0
        return 1

    def _score(account: str) -> tuple[int, int, int]:
        # Higher scores rank first. Tuple sorts lexicographically.
        candidate_bonus = 5 if account in candidate_accounts else 0
        return (candidate_bonus, _sign_score(account), counter[account])

    ranked = sorted(all_accounts, key=lambda a: _score(a), reverse=True)
    top: list[str] = []
    if suggested_target:
        top.append(suggested_target)
    for acc in ranked:
        if acc == suggested_target:
            continue
        top.append(acc)
        if len(top) >= top_n:
            break
    return top, all_accounts
