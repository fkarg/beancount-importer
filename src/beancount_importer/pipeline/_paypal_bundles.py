"""PayPal bundle collapsing — a post-parse pass over parsed transactions.

PayPal splits some activity across several linked CSV rows that only make
sense together, so this runs *after* parsing (on `SourceTransaction`s, using
their preserved `raw_data`) rather than in a streaming parser. Applied only
to the `paypal` bank; see `_parse_all_inputs`.

Two shapes are handled, both linked by the `Reference Txn ID` column:

- **Foreign-currency purchase** — a payment in a foreign currency plus two
  "General Currency Conversion" legs (one in the payment's currency, the FX
  swap; one in the home currency, the real balance change). These collapse
  into a single transaction booking the home amount, with the foreign amount
  carried in `original_amount`/`original_currency` so the writer prices the
  counter-leg `<foreign> @@ <home>`. The two GCC legs are dropped.

- **Authorization hold noise** — an "Account Hold for Open Authorization" and
  its "Reversal of General Account Hold" form an exact ±X mirror pair that
  nets to zero; the real charge posts separately. Matched pairs are dropped.

Anything that doesn't match a shape exactly is passed through untouched — no
row is silently lost.
"""

from __future__ import annotations

from collections import defaultdict

from beancount_importer.models import SourceTransaction

# Raw-column keys (English + German), read off `raw_data`. The generic parser
# preserves the full row, so grouping keys survive regardless of field mapping.
_TXNID_KEYS = ("Transaction ID", "Transaktionscode")
_REFTXN_KEYS = ("Reference Txn ID", "Zugehöriger Transaktionscode")
_DESC_KEYS = ("Description", "Subject", "Betreff", "Item Title")

_GCC_DESC = "General Currency Conversion"
_HOLD_DESC = "Account Hold for Open Authorization"
_REVERSAL_DESC = "Reversal of General Account Hold"


def _raw(txn: SourceTransaction, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = txn.raw_data.get(k)
        if v:
            return v.strip()
    return ""


def collapse_paypal_bundles(
    txns: list[SourceTransaction],
) -> list[SourceTransaction]:
    """Collapse currency-conversion bundles and drop hold/reversal noise."""
    by_id = {tid: t for t in txns if (tid := _raw(t, _TXNID_KEYS))}
    suppress, overrides = _plan_currency_conversions(txns, by_id)
    suppress |= _plan_hold_reversals(txns, by_id)

    out: list[SourceTransaction] = []
    for t in txns:
        tid = _raw(t, _TXNID_KEYS)
        if tid and tid in suppress:
            continue
        if tid in overrides:
            t = t.model_copy(update=overrides[tid])
        out.append(t)
    return out


def _plan_currency_conversions(
    txns: list[SourceTransaction],
    by_id: dict[str, SourceTransaction],
) -> tuple[set[str], dict[str, dict[str, object]]]:
    """Return (GCC-leg ids to drop, payment-id → collapsed-field overrides).

    A bundle qualifies only when a payment has exactly one home-currency and
    one same-currency GCC leg; anything else is left untouched.
    """
    legs_by_parent: dict[str, list[SourceTransaction]] = defaultdict(list)
    for t in txns:
        if _raw(t, _DESC_KEYS) == _GCC_DESC:
            legs_by_parent[_raw(t, _REFTXN_KEYS)].append(t)

    suppress: set[str] = set()
    overrides: dict[str, dict[str, object]] = {}
    for parent_id, legs in legs_by_parent.items():
        parent = by_id.get(parent_id)
        if parent is None:
            # Empty ref, or a parent outside this file — can't collapse.
            continue
        home = [leg for leg in legs if leg.currency != parent.currency]
        foreign = [leg for leg in legs if leg.currency == parent.currency]
        if len(home) != 1 or len(foreign) != 1:
            continue
        overrides[parent_id] = {
            "amount": home[0].amount,
            "currency": home[0].currency,
            "original_amount": abs(parent.amount),
            "original_currency": parent.currency,
        }
        suppress.update(_raw(leg, _TXNID_KEYS) for leg in legs)
    return suppress, overrides


def _plan_hold_reversals(
    txns: list[SourceTransaction],
    by_id: dict[str, SourceTransaction],
) -> set[str]:
    """Return ids of hold↔reversal mirror pairs to drop.

    Only an exact match is dropped: the reversal must reference a hold row
    whose amount is the exact negation. An unpaired hold is kept — a hold that
    became a real charge must not vanish.
    """
    suppress: set[str] = set()
    for t in txns:
        if _raw(t, _DESC_KEYS) != _REVERSAL_DESC:
            continue
        hold = by_id.get(_raw(t, _REFTXN_KEYS))
        if hold is None or _raw(hold, _DESC_KEYS) != _HOLD_DESC:
            continue
        if t.amount != -hold.amount:
            continue
        suppress.update({_raw(t, _TXNID_KEYS), _raw(hold, _TXNID_KEYS)})
    return suppress
