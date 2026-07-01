"""Building `ImportResult` from a finalised proposal + matching context.

Everything in here is pure: no DecisionLog writes, no Reporter calls, no
state threading. `_build_result` is the only entry point used by run.py;
the rest are helpers it delegates to (diffing, new-entry formatting,
cross-bank counter-leg synthesis, date-metadata proposals).

Kept separate from run.py so the pipeline's flow code isn't interleaved
with field-level diff suppression rules.
"""

from __future__ import annotations

import re

from beancount_importer.beancount_io.writer import format_transaction
from beancount_importer.config import BankConfig
from beancount_importer.matching.scorer import find_candidates
from beancount_importer.models import (
    CategoryProposal,
    ImportResult,
    LedgerEntry,
    Posting,
    ProposedChange,
    SourceTransaction,
)
from beancount_importer.rules.models import CategorizationRule
from beancount_importer.rules.tags import TagStateDelta


def _build_result(
    *,
    txn: SourceTransaction,
    bank: BankConfig,
    proposal: CategoryProposal,
    existing: list[LedgerEntry],
    matched_rule: CategorizationRule | None,
    is_replay: bool,
    new_rule: CategorizationRule | None,
    replaced_rule: CategorizationRule | None = None,
    tag_state_delta: TagStateDelta | None,
    min_score: float,
    max_date_days: int,
    match_txn: SourceTransaction | None = None,
    narration_max_length: int | None = None,
    paypal_account: str | None = None,
) -> ImportResult:
    """Assemble the per-row result.

    `match_txn` carries any rule-driven payee/narration overrides used during
    candidate scoring; if omitted (e.g. on the replay path that doesn't
    pre-rewrite), we fall back to the raw `txn`. The split lets the call site
    own the rule semantics without duplicating overrides logic here.
    """
    if proposal.action == "skip":
        return ImportResult(
            source_txn=txn,
            action="skip",
            proposal=proposal,
            rule_matched=matched_rule,
            is_replay=is_replay,
        )
    if proposal.action == "quit":
        return ImportResult(
            source_txn=txn,
            action="quit",
            proposal=proposal,
            rule_matched=matched_rule,
            is_replay=is_replay,
        )

    # categorize action: decide new vs. update by inspecting top candidate.
    score_txn = match_txn if match_txn is not None else txn
    candidates = find_candidates(
        score_txn, existing, min_score=min_score, max_date_days=max_date_days
    )
    best: LedgerEntry | None = candidates[0][0] if candidates else None
    action = "update" if best is not None else "new"

    proposed_changes: list[ProposedChange] = []
    new_entry_text = ""
    if best is not None:
        if not best.amount_inferred:
            proposed_changes = _diff_changes(best, proposal, matched_rule)
        elif best.metadata.get("_pending_in_session"):
            # Same-session placeholder: run.py folds the date hint onto leg
            # 1's pending entry and clears the changes; the placeholder
            # itself is never spliced.
            proposed_changes, proposal = _propose_date_metadata(
                txn, best, proposal, paypal_account=paypal_account
            )
        elif _collapse_applicable(txn, best, paypal_account):
            proposed_changes, proposal = _propose_collapse(txn, best, proposal)
        else:
            # Deposit/transit legs, synthesized entries, multi-posting
            # transfers: the row is settled by the match, nothing to write.
            # A standard re-render of an inferred entry would rebuild the
            # transaction around the wrong side's amount and destroy the
            # counter leg (sign-inversion corruption) — no other update
            # shape is permitted here.
            proposed_changes = []
    else:
        new_entry_text = _format_new_entry(
            bank, txn, proposal, narration_max_length=narration_max_length
        )

    return ImportResult(
        source_txn=txn,
        action=action,  # type: ignore[arg-type]
        matched_entry=best,
        proposed_changes=proposed_changes,
        new_entry_text=new_entry_text,
        proposal=proposal,
        rule_matched=matched_rule,
        is_replay=is_replay,
        new_rule=new_rule,
        replaced_rule=replaced_rule,
        tag_state_delta=tag_state_delta,
    )


def _synthesize_counter_leg(
    result: ImportResult,
    txn: SourceTransaction,
    *,
    internal_prefixes: tuple[str, ...],
    bank_accounts: set[str],
) -> LedgerEntry | None:
    """Create an inferred-amount LedgerEntry mirroring a cross-bank transfer.

    Fires when `_process_transaction` returns `action="new"` and the
    proposal's target account looks like another configured bank (or
    matches any `internal_transfer_account_prefixes`). The synthesized
    entry sits in the *target* bank's bucket so subsequent CSV rows
    from that bank match it via the scorer's `amount_inferred` path
    (rather than the user being prompted again for the same flow).

    `amount_inferred=True` is the contract that makes
    `_propose_date_metadata` fire when the second bank's CSV date
    disagrees with the first leg's booking date.
    """
    if result.action != "new" or result.proposal is None:
        return None
    target = result.proposal.target_account
    if not target:
        return None
    if target not in bank_accounts and not target.startswith(internal_prefixes):
        return None
    return LedgerEntry(
        date=txn.value_date or txn.booking_date,
        narration=result.proposal.narration or txn.description or "",
        payee=result.proposal.payee or txn.payee,
        source_account=target,
        target_account="",
        amount=-txn.amount,
        currency=txn.currency,
        amount_inferred=True,
        metadata={"_pending_in_session": "true"},
        file_path="",
        line_start=0,
    )


def _fold_inflight_date_hint(
    origin: ImportResult,
    hint_account: str,
    changes: list[ProposedChange],
    bank: BankConfig,
    *,
    narration_max_length: int | None = None,
) -> ImportResult:
    """Fold leg-2's proposed date metadata onto leg-1's not-yet-written entry.

    When both legs of a cross-bank transfer are imported in one run, leg 2
    matches the in-session placeholder seeded by `_synthesize_counter_leg`.
    That placeholder has no on-disk location, so the `settle`/`actual`/`paypal`
    date hint it proposes can't be spliced — it belongs on leg 1's freshly
    formatted entry instead. We add the hint to the posting whose account is
    `hint_account` (the counter-party bank leg) and re-render `new_entry_text`.

    `changes` carries leg 2's `posting:<key>` proposed changes (the only kind
    `_propose_date_metadata` emits); any non-`posting:` change is ignored. The
    caller only invokes this for a leg-1 "new" result, which always carries a
    proposal.
    """
    assert origin.proposal is not None  # leg-1 "new" result always has one
    hints = {
        change.field.split(":", 1)[1]: change.new_val
        for change in changes
        if change.field.startswith("posting:")
    }
    updated_postings = [
        p.model_copy(update={"metadata": {**p.metadata, **hints}})
        if p.account == hint_account
        else p
        for p in origin.proposal.postings
    ]
    amended = origin.proposal.model_copy(update={"postings": tuple(updated_postings)})
    new_text = _format_new_entry(
        bank, origin.source_txn, amended, narration_max_length=narration_max_length
    )
    return origin.model_copy(
        update={"proposal": amended, "new_entry_text": new_text}
    )


def _collapse_applicable(
    txn: SourceTransaction,
    entry: LedgerEntry,
    paypal_account: str | None,
) -> bool:
    """True when `txn` is the PayPal-side *purchase* of an on-disk cross-bank
    transfer leg — the only shape `_propose_collapse` rewrites.

    Reversed sign is the purchase signature: the CSV row spends what the
    transfer's inferred leg received. Same-sign matches are the deposit/
    transit side and stay silent. Synthesized and in-session entries have no
    on-disk transaction of their own to collapse; multi-posting transfers
    carry user-authored structure a rewrite would destroy.
    """
    return (
        paypal_account is not None
        and entry.amount_inferred
        and entry.source_account == paypal_account
        and bool(entry.file_path)
        and not entry.has_multiple_postings
        and "synthesized_from" not in entry.metadata
        and "_pending_in_session" not in entry.metadata
        and txn.amount == -entry.amount
    )


def _propose_collapse(
    txn: SourceTransaction,
    entry: LedgerEntry,
    proposal: CategoryProposal,
) -> tuple[list[ProposedChange], CategoryProposal]:
    """Rewrite a PayPal pass-through into the collapsed via-paypal form.

    The matched entry is the transfer's inferred PayPal-side leg. The
    rewritten transaction keeps the funding leg verbatim — `target_account`
    with the negated inferred amount — stamped with posting-level
    `paypal: <CSV date>` so the user's `settle_inv` plugin splits it at load
    time and re-imports recognise the row as settled (settled matcher /
    reader synthesis both read posting metadata only). The returned
    proposal's postings are the *complete* posting list; persistence renders
    them via `apply_update(collapse=True)` without the implicit bank leg.

    A proposal that targets the funding account itself means the user (or
    the silent seed) categorized the row as the transfer — nothing to
    collapse — unless it already carries `paypal:` metadata, which marks a
    replayed collapse decision whose funding leg must not be duplicated.
    """
    if not proposal.postings:
        return [], proposal
    first = proposal.postings[0]
    pp_date = (txn.value_date or txn.booking_date).isoformat()
    if first.account == entry.target_account:
        if "paypal" not in first.metadata:
            return [], proposal
        updated = proposal
        pp_date = first.metadata["paypal"]
    else:
        funding = Posting(
            account=entry.target_account,
            amount=-entry.amount,
            currency=entry.currency,
            metadata={"paypal": pp_date},
        )
        updated = proposal.model_copy(
            update={"postings": (funding, *proposal.postings)}
        )
    expense = next(
        (p.account for p in updated.postings if p.account != entry.target_account),
        "",
    )
    return (
        [
            ProposedChange("target_account", entry.source_account, expense),
            ProposedChange("posting:paypal", "", pp_date),
        ],
        updated,
    )


def _propose_date_metadata(
    txn: SourceTransaction,
    entry: LedgerEntry,
    proposal: CategoryProposal,
    *,
    paypal_account: str | None,
) -> tuple[list[ProposedChange], CategoryProposal]:
    """For amount_inferred (cross-bank transit) matches, propose date
    metadata on the matched leg's posting when the CSV and ledger
    dates disagree. Returns the (possibly empty) ProposedChange list
    plus a (possibly mutated) proposal carrying the new metadata.

    Routing:

    | CSV date vs entry.date | Matched account is `paypal_account`? | Key      |
    |  Earlier               | Yes                                  | `paypal` |
    |  Earlier               | No                                   | `actual` |
    |  Later                 | (n/a)                                | `settle` |
    |  Equal                 | (n/a)                                | (none)   |

    The metadata sits on the matched-entry's posting — the inferred
    leg, which is the one that needs the alternate-date hint so the
    user's plugin moves the posting back to the CSV's recorded date.
    The writer renders posting-level metadata indented under the
    posting line per Phase 1.
    """
    csv_date = txn.value_date or txn.booking_date
    if csv_date == entry.date:
        return [], proposal

    if csv_date > entry.date:
        key = "settle"
    elif paypal_account is not None and entry.source_account == paypal_account:
        key = "paypal"
    else:
        key = "actual"

    new_value = csv_date.isoformat()

    # The proposal's first posting is the target_account (the inferred
    # leg's account). Attach the metadata there. If the key already
    # carries the same value, no change is proposed.
    existing_value = ""
    if proposal.postings and key in proposal.postings[0].metadata:
        existing_value = proposal.postings[0].metadata[key]
    if existing_value == new_value:
        return [], proposal

    updated_postings: list[Posting] = []
    for i, p in enumerate(proposal.postings):
        if i == 0:
            updated_postings.append(
                p.model_copy(update={"metadata": {**p.metadata, key: new_value}})
            )
        else:
            updated_postings.append(p)
    updated = proposal.model_copy(update={"postings": tuple(updated_postings)})
    return (
        [ProposedChange(field=f"posting:{key}", old_val=existing_value, new_val=new_value)],
        updated,
    )


_TIMESTAMP_NARRATION_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?"
    r"\s*(Debit|Credit|Lastschrift|Gutschrift)?\s*$"
)


def _is_truncation_equivalent(proposal: str, existing: str) -> bool:
    """True when one string is a prefix of the other (after rstrip).

    The writer silently truncates narrations to `narration_max_length`;
    re-importing the same row with the original (longer) narration must
    not register as a field change. Symmetric: the existing entry might
    be the longer one if the user lowered the truncation length.
    """
    p = proposal.rstrip()
    e = existing.rstrip()
    if not p or not e:
        return False
    return p.startswith(e) or e.startswith(p)


def _diff_changes(
    entry: LedgerEntry,
    proposal: CategoryProposal,
    rule: CategorizationRule | None,
) -> list[ProposedChange]:
    """Compute the field-level delta between an existing entry and a new proposal,
    honoring per-rule suppression flags."""

    changes: list[ProposedChange] = []
    suppress_all = rule.suppress_updates if rule else False
    if suppress_all:
        return []

    if proposal.payee and proposal.payee != (entry.payee or ""):
        if not (rule and rule.suppress_payee_updates):
            changes.append(ProposedChange("payee", entry.payee or "", proposal.payee))

    if proposal.narration and proposal.narration != entry.narration:
        # A1/A8: a re-imported CSV row whose narration was previously
        # truncated at write time differs by suffix only. Don't propose
        # rewinding the truncation as a field change.
        if not _is_truncation_equivalent(proposal.narration, entry.narration):
            # A2: bare CSV `Type`-shaped values (timestamp + Debit/Credit)
            # are pure transport metadata, not anything the user typed.
            # Suppress the overwrite when the existing entry already has
            # a real narration ("Burger King") and no rule forces it.
            timestamp_proposal = bool(
                _TIMESTAMP_NARRATION_RE.match(proposal.narration)
            )
            real_existing = bool(
                entry.narration and not _TIMESTAMP_NARRATION_RE.match(entry.narration)
            )
            if not (timestamp_proposal and real_existing and rule is None):
                if not (rule and rule.suppress_narration_updates):
                    changes.append(
                        ProposedChange("narration", entry.narration, proposal.narration)
                    )

    if proposal.target_account and proposal.target_account != entry.target_account:
        # A4: salary / multi-leg entries are user-authored structures; a
        # single CSV row should not rewrite the merchant-side account
        # away from whatever the user spread across the deduction legs.
        if entry.has_multiple_postings:
            return changes
        if not (rule and rule.suppress_account_updates):
            changes.append(
                ProposedChange("account", entry.target_account, proposal.target_account)
            )

    return changes


def _foreign_price_str(txn: SourceTransaction) -> str | None:
    """`@@`-priced counter-leg amount for a collapsed foreign purchase, or None.

    The home leg is `txn.amount` (e.g. ``-25.03 EUR``); the counter-leg is the
    original foreign amount priced at the home total: ``25.95 USD @@ 25.03 EUR``.
    The counter-leg's sign is the opposite of the home leg (a purchase debits
    the bank and credits — positive — the expense).

    Gated on a *positive* `original_amount`: the PayPal collapse stores the
    foreign amount as a positive magnitude, whereas the generic/N26 parser
    stores a signed raw value (negative for debits). This keeps `@@` rendering
    PayPal-only for now — N26 multi-currency stays as-is until we opt it in.
    """
    if (
        txn.original_amount is None
        or txn.original_currency is None
        or txn.original_amount <= 0
    ):
        return None
    foreign = txn.original_amount if txn.amount < 0 else -txn.original_amount
    return f"{foreign} {txn.original_currency} @@ {abs(txn.amount)} {txn.currency}"


def _format_new_entry(
    bank: BankConfig,
    txn: SourceTransaction,
    proposal: CategoryProposal,
    narration_max_length: int | None = None,
) -> str:
    """Render a new beancount transaction text from the proposal."""
    payee = proposal.payee or txn.payee
    narration = proposal.narration or txn.description or ""

    postings: list[tuple[str, str | None, dict[str, str]]] = []
    # Source-account leg always carries the explicit amount + currency.
    postings.append(
        (bank.account, f"{txn.amount} {txn.currency}", {})
    )
    # A collapsed foreign-currency purchase (e.g. PayPal's General Currency
    # Conversion bundle) books the home amount on the source leg and prices
    # the single counter-leg in the original currency with a `@@` total cost.
    foreign = _foreign_price_str(txn)
    price_single_leg = (
        foreign is not None
        and len(proposal.postings) == 1
        and proposal.postings[0].amount is None
    )
    for p in proposal.postings:
        amount_str: str | None = None
        if price_single_leg:
            amount_str = foreign
        elif p.amount is not None:
            currency = p.currency or txn.currency
            amount_str = f"{p.amount} {currency}"
        postings.append((p.account, amount_str, dict(p.metadata)))

    metadata = dict(proposal.metadata)
    # `#hashtag` on the header, not `tag:` metadata — see format_transaction.
    tags = (proposal.tag,) if proposal.tag else ()

    return format_transaction(
        date_str=txn.booking_date.isoformat(),
        flag="*",
        payee=payee,
        narration=narration,
        postings=postings,
        metadata=metadata,
        tags=tags,
        narration_max_length=narration_max_length,
    )
