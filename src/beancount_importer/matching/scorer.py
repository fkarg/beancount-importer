from __future__ import annotations

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from beancount_importer.matching.normalize import normalize_text
from beancount_importer.models import LedgerEntry, SourceTransaction


# Tunable: candidates further apart than this are not considered.
DEFAULT_MAX_DATE_DAYS = 14
# Score weights: text contributes the most, date proximity is a tie-breaker.
TEXT_WEIGHT = 0.7
DATE_WEIGHT = 0.3
SEPA_BONUS = 0.5  # added (then clipped to 1.0) when SEPA refs match exactly


def similarity_score(a: str, b: str) -> float:
    """Token-set ratio on normalized strings, 0–100."""
    na, nb = normalize_text(a), normalize_text(b)
    return fuzz.token_set_ratio(na, nb)


def levenshtein_distance(a: str, b: str) -> int:
    """Edit distance on normalized strings."""
    return Levenshtein.distance(normalize_text(a), normalize_text(b))


def lcs_length(a: str, b: str) -> int:
    """Longest common subsequence length on normalized strings."""
    na, nb = normalize_text(a), normalize_text(b)
    m, n = len(na), len(nb)
    if m == 0 or n == 0:
        return 0
    # DP, O(m*n) — fine for short strings like transaction descriptions
    prev = [0] * (n + 1)
    for ch in na:
        curr = [0] * (n + 1)
        for j, bch in enumerate(nb):
            curr[j + 1] = prev[j] + 1 if ch == bch else max(curr[j], prev[j + 1])
        prev = curr
    return prev[n]


def score_candidate(
    txn: SourceTransaction,
    entry: LedgerEntry,
    *,
    max_date_days: int = DEFAULT_MAX_DATE_DAYS,
    include_reversed_sign: bool = False,
) -> float:
    """Score how likely `entry` is the same transaction as `txn`, in [0.0, 1.0].

    Hard filters return 0.0 immediately:
    - currency mismatch
    - amount mismatch (signs must agree unless `include_reversed_sign`)
    - date difference > max_date_days

    Otherwise: weighted combination of text similarity (payee+narration vs.
    payee+description) and date proximity, plus a SEPA-reference bonus.
    """
    if txn.currency != entry.currency:
        return 0.0
    if include_reversed_sign:
        if abs(txn.amount) != abs(entry.amount):
            return 0.0
    elif txn.amount != entry.amount:
        return 0.0

    days_apart = abs((txn.booking_date - entry.date).days)
    if days_apart > max_date_days:
        return 0.0

    txn_text = " ".join(filter(None, [txn.payee, txn.description]))
    entry_text = " ".join(filter(None, [entry.payee, entry.narration]))
    text = similarity_score(txn_text, entry_text) / 100.0
    date_proximity = 1.0 - (days_apart / max_date_days)
    score = TEXT_WEIGHT * text + DATE_WEIGHT * date_proximity

    sepa = entry.metadata.get("sepa_ref") or entry.metadata.get("sepa_reference", "")
    if txn.sepa_reference and txn.sepa_reference == sepa:
        score = min(1.0, score + SEPA_BONUS)

    return score


def find_candidates(
    txn: SourceTransaction,
    entries: list[LedgerEntry],
    *,
    min_score: float = 0.0,
    max_date_days: int = DEFAULT_MAX_DATE_DAYS,
    include_reversed_sign: bool = False,
) -> list[tuple[LedgerEntry, float]]:
    """Score `entries` against `txn` and return those above `min_score`, sorted desc."""
    scored: list[tuple[LedgerEntry, float]] = []
    for entry in entries:
        s = score_candidate(
            txn,
            entry,
            max_date_days=max_date_days,
            include_reversed_sign=include_reversed_sign,
        )
        if s >= min_score and s > 0.0:
            scored.append((entry, s))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored
