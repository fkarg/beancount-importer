from __future__ import annotations

from datetime import date as _date, datetime as _datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from beancount.loader import load_file
from beancount.core import data as bc_data

from beancount_importer.models import LedgerEntry


# Default keys for posting-level metadata whose value is an alternate
# transaction date. Reader-level fallback; the canonical list lives in
# `MatchingConfig.metadata_date_keys` and is passed in explicitly when the
# pipeline calls `read_ledger`.
_DEFAULT_METADATA_DATE_KEYS = ("actual", "paypal", "settle")


def read_ledger(
    path: Path,
    source_account: str,
    *,
    metadata_date_keys: Iterable[str] = _DEFAULT_METADATA_DATE_KEYS,
    synthesize_from_metadata: dict[str, str] | None = None,
) -> list[LedgerEntry]:
    """Load all Transaction entries from a .bean file for the given source account.

    `synthesize_from_metadata` optionally maps a posting-level metadata key
    (e.g. `paypal`) to an account name. When a posting on a transaction
    carries that metadata and the mapped account equals `source_account`,
    a virtual LedgerEntry is added — modelling the entry that the user's
    beancount plugin would synthesize at load time. See
    `_synthesize_virtual_entry` for the conventions.
    """
    if not path.exists():
        return []

    entries, _errors, _options = load_file(str(path))
    results: list[LedgerEntry] = []

    date_keys = tuple(metadata_date_keys)
    synth_map = synthesize_from_metadata or {}
    for entry in entries:
        if not isinstance(entry, bc_data.Transaction):
            continue
        natural = _extract_entry(entry, source_account, str(path), date_keys)
        if natural is not None:
            results.append(natural)
        for synthesized in _synthesize_virtual_entries(
            entry, source_account, str(path), synth_map
        ):
            results.append(synthesized)

    return results


def _extract_entry(
    txn: bc_data.Transaction,
    source_account: str,
    file_path: str,
    metadata_date_keys: tuple[str, ...],
) -> LedgerEntry | None:
    """Convert a beancount Transaction to LedgerEntry, keyed by source_account."""
    source_posting = None
    target_posting = None

    for posting in txn.postings:
        if posting.account == source_account:
            source_posting = posting
        else:
            if target_posting is None:
                target_posting = posting

    if source_posting is None or source_posting.units is None:
        return None

    amount = Decimal(str(source_posting.units.number))
    currency = source_posting.units.currency
    target_account = target_posting.account if target_posting else ""

    # An inferred posting amount is a strong signal that this entry is the
    # cross-bank "transit" leg of someone else's primary transaction (e.g.,
    # SPK -10 EUR / PayPal {inferred +10}). Beancount's loader fills in the
    # missing number; the AST exposes this via `posting.meta["__automatic__"]`.
    amount_inferred = bool(
        source_posting.meta and source_posting.meta.get("__automatic__")
    )

    meta = dict(txn.meta)
    line_start = meta.pop("lineno", 0)

    # Collect posting-level metadata (e.g. sepa_ref, actual, paypal) and any
    # alternate dates the user's plugins recognise.
    metadata_dates: list[_date] = []
    for posting in txn.postings:
        if not posting.meta:
            continue
        for k, v in posting.meta.items():
            if k in ("filename", "lineno") or k.startswith("__"):
                continue
            if k in metadata_date_keys:
                parsed = _coerce_date(v)
                if parsed is not None:
                    metadata_dates.append(parsed)
            if k not in meta:
                meta[k] = str(v)

    meta.pop("filename", None)
    meta.pop("lineno", None)

    return LedgerEntry(
        date=txn.date,
        flag=txn.flag,
        payee=txn.payee,
        narration=txn.narration,
        source_account=source_account,
        target_account=target_account,
        amount=amount,
        currency=currency,
        metadata={k: str(v) for k, v in meta.items()},
        line_start=line_start,
        file_path=file_path,
        amount_inferred=amount_inferred,
        metadata_dates=tuple(dict.fromkeys(metadata_dates)),  # de-dup, preserve order
    )


def _synthesize_virtual_entries(
    txn: bc_data.Transaction,
    source_account: str,
    file_path: str,
    synthesize_from_metadata: dict[str, str],
) -> list[LedgerEntry]:
    """Reconstruct entries that a user-installed plugin would generate.

    For each posting carrying a metadata key in `synthesize_from_metadata`
    where the mapped account equals `source_account`, emit a LedgerEntry
    that mirrors what the plugin would produce at load time:

    - date: the metadata value (parsed as a date)
    - amount: same as the metadata-bearing posting (the plugin moves that
      amount to the synthesized account)
    - target_account: the *other* posting's account, since the plugin
      effectively re-pairs the synthesized leg with the transaction's
      non-bank leg (e.g. the merchant's `Expenses:*`)
    - amount_inferred=True: marks this as a transit/cross-bank entry so
      the scorer allows reversed-sign matches and the pipeline doesn't
      propose merge changes against it
    """
    if not synthesize_from_metadata:
        return []

    out: list[LedgerEntry] = []
    for src_posting in txn.postings:
        # beancount's loader populates `posting.meta` (with at least
        # filename/lineno) and `posting.units` (with the explicit or inferred
        # amount) for every posting on a Transaction. The None-checks below
        # are defensive only — they exist to satisfy the static-type narrowing
        # because the API types both attributes as `Optional[...]`.
        if src_posting.meta is None or src_posting.units is None:  # pragma: no cover
            continue
        for key, mapped in synthesize_from_metadata.items():
            if mapped != source_account:
                continue
            raw = src_posting.meta.get(key)
            if raw is None:
                continue
            metadata_date = _coerce_date(raw)
            if metadata_date is None:
                continue
            # Pick the most plausible "other side": prefer non-Asset/Liability
            # accounts (typically Expenses/Income), falling back to any
            # posting that isn't the metadata-bearing one. This mirrors the
            # split the plugin produces — the synthesized leg is paired
            # with the merchant/income leg, not the original bank leg.
            other = _pick_other_posting(txn.postings, src_posting)
            target_account = other.account if other else ""

            amount = Decimal(str(src_posting.units.number))
            currency = src_posting.units.currency
            out.append(
                LedgerEntry(
                    date=metadata_date,
                    flag=txn.flag,
                    payee=txn.payee,
                    narration=txn.narration,
                    source_account=source_account,
                    target_account=target_account,
                    amount=amount,
                    currency=currency,
                    metadata={"synthesized_from": key},
                    line_start=int(txn.meta.get("lineno", 0)) if txn.meta else 0,
                    file_path=file_path,
                    amount_inferred=True,
                    metadata_dates=(metadata_date,),
                )
            )
    return out


def _pick_other_posting(postings, source_posting):
    """Choose the posting most likely to be the merchant/income side.

    Prefers Expenses:/Income: postings; otherwise the first non-source
    posting; otherwise None.
    """
    expense_or_income = None
    fallback = None
    for p in postings:
        if p is source_posting:
            continue
        if p.account.startswith(("Expenses:", "Income:")):
            if expense_or_income is None:
                expense_or_income = p
        elif fallback is None:
            fallback = p
    return expense_or_income or fallback


def _coerce_date(value) -> _date | None:
    """Best-effort parse of a metadata value into a date.

    Beancount stores metadata dates as `datetime.date` already; user-typed
    strings (e.g. raw `paypal: "2024-01-17"`) come through as `str`.
    """
    if isinstance(value, _date) and not isinstance(value, _datetime):
        return value
    if isinstance(value, _datetime):
        return value.date()
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
            try:
                return _datetime.strptime(value.strip().strip('"'), fmt).date()
            except ValueError:
                continue
    return None
