from __future__ import annotations

from datetime import date as _date, datetime as _datetime
from decimal import Decimal
from pathlib import Path
from collections.abc import Iterable

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
    return read_ledger_multi(
        path,
        accounts=(source_account,),
        metadata_date_keys=metadata_date_keys,
        synthesize_from_metadata=synthesize_from_metadata,
    )


def read_open_accounts(path: Path) -> frozenset[str]:
    """Return every account named by an `open` directive reachable from `path`.

    `load_file` resolves `include`s, so pointing this at a standalone
    `accounts.bean` (or a top-level `main.bean` that pulls the chart in
    transitively) yields the full chart — crucially including accounts that
    carry no transaction yet. The transaction-derived pool
    (`read_ledger_multi`) can only surface accounts that appear on a posting,
    so unused-but-opened accounts are invisible without this.
    """
    if not path.exists():
        return frozenset()
    entries, _errors, _options = load_file(str(path))
    return frozenset(
        d.account for d in entries if isinstance(d, bc_data.Open)
    )


def read_ledger_multi(
    path: Path,
    *,
    accounts: Iterable[str] | None = None,
    account_prefixes: Iterable[str] | None = None,
    metadata_date_keys: Iterable[str] = _DEFAULT_METADATA_DATE_KEYS,
    synthesize_from_metadata: dict[str, str] | None = None,
) -> list[LedgerEntry]:
    """Read every bank-shaped posting in `path`, one LedgerEntry per match.

    A posting is "in scope" when its account is in `accounts` or starts with
    any prefix in `account_prefixes`. At least one of the two must be a
    non-empty iterable; otherwise the call returns `[]`. A Transaction with
    two in-scope legs (e.g. an SPK→PayPal transfer) yields two entries with
    distinct `source_account` values.

    Metadata synthesis still applies, but only for mapped accounts that are
    themselves in scope — so a `paypal: 2024-01-17` hint synthesises a
    PayPal-side LedgerEntry only when `Assets:B:PayPal` matches.
    """
    if not path.exists():
        return []

    explicit = set(accounts or ())
    prefixes = tuple(account_prefixes or ())
    if not explicit and not prefixes:
        return []

    def in_scope(account: str) -> bool:
        if account in explicit:
            return True
        return bool(prefixes) and account.startswith(prefixes)

    entries, _errors, _options = load_file(str(path))
    results: list[LedgerEntry] = []
    date_keys = tuple(metadata_date_keys)
    synth_map = synthesize_from_metadata or {}

    for txn in entries:
        if not isinstance(txn, bc_data.Transaction):
            continue
        seen_accounts: set[str] = set()
        for posting in txn.postings:
            if posting.account in seen_accounts or not in_scope(posting.account):
                continue
            seen_accounts.add(posting.account)
            natural = _extract_entry(txn, posting.account, str(path), date_keys)
            if natural is not None:  # pragma: no branch  # pre-filtered
                results.append(natural)
        for mapped in {m for m in synth_map.values() if in_scope(m)}:
            results.extend(
                _synthesize_virtual_entries(txn, mapped, str(path), synth_map)
            )

    return results


def _extract_entry(
    txn: bc_data.Transaction,
    source_account: str,
    file_path: str,
    metadata_date_keys: tuple[str, ...],
) -> LedgerEntry | None:
    """Convert a beancount Transaction to LedgerEntry, keyed by source_account."""
    source_posting: bc_data.Posting | None = None
    target_posting: bc_data.Posting | None = None

    for posting in txn.postings:
        if posting.account == source_account:
            source_posting = posting
        elif target_posting is None:
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

    # Drop beancount's reserved loader keys (`__tolerances__`, `__residual__`,
    # …) from the txn-level meta — they are not user metadata and are invalid
    # as source syntax, so carrying them onto the entry would make a later
    # `apply_update` splice unparseable. (Posting-level meta is filtered below.)
    meta = {k: v for k, v in txn.meta.items() if not k.startswith("__")}
    line_start = meta.pop("lineno", 0)
    # Beancount's loader records the *real* source file in `meta["filename"]`
    # — different from `file_path` when the entry was reached via an
    # `include` directive in a wrapper file. Using the real source means
    # callers can dedupe entries that surface multiple times (once
    # directly, once via `main.bean`, once via `all.bean`, etc.).
    real_filename = meta.pop("filename", None)
    resolved_path = str(real_filename) if real_filename else file_path

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

    meta.pop("lineno", None)

    return LedgerEntry(
        date=txn.date,
        flag=txn.flag or "*",
        payee=txn.payee,
        narration=txn.narration or "",
        source_account=source_account,
        target_account=target_account,
        amount=amount,
        currency=currency,
        metadata={k: str(v) for k, v in meta.items()},
        line_start=line_start,
        file_path=resolved_path,
        amount_inferred=amount_inferred,
        metadata_dates=tuple(dict.fromkeys(metadata_dates)),  # de-dup, preserve order
        has_multiple_postings=len(txn.postings) > 2,
        tags=tuple(sorted(txn.tags or ())),
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
            # Use the posting's own filename/lineno: it points to the
            # *actual* source file (across `include` wrappers) and to a
            # distinct line per posting, so different synthesized entries
            # from the same parent transaction get distinct identities.
            posting_filename = src_posting.meta.get("filename") if src_posting.meta else None
            posting_lineno = src_posting.meta.get("lineno") if src_posting.meta else None
            out.append(
                LedgerEntry(
                    date=metadata_date,
                    flag=txn.flag or "*",
                    payee=txn.payee,
                    narration=txn.narration or "",
                    source_account=source_account,
                    target_account=target_account,
                    amount=amount,
                    currency=currency,
                    metadata={"synthesized_from": key},
                    line_start=int(posting_lineno) if posting_lineno else 0,
                    file_path=str(posting_filename) if posting_filename else file_path,
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
