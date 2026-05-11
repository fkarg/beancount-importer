from __future__ import annotations

from decimal import Decimal
from datetime import date
from typing import TYPE_CHECKING, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from beancount_importer.rules.models import CategorizationRule
    from beancount_importer.rules.tags import TagStateDelta


class SourceTransaction(BaseModel):
    model_config = ConfigDict(frozen=True)

    booking_date: date
    value_date: date | None = None
    amount: Decimal
    currency: str = "EUR"
    description: str | None = None
    payee: str | None = None
    bank_key: str
    sepa_reference: str = ""
    raw_data: dict[str, str] = {}
    original_amount: Decimal | None = None
    original_currency: str | None = None
    exchange_rate: Decimal | None = None


class LedgerEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: date
    flag: str = "*"
    payee: str | None = None
    narration: str
    source_account: str
    target_account: str
    amount: Decimal
    currency: str = "EUR"
    metadata: dict[str, str] = {}
    line_start: int = 0
    line_end: int = 0
    file_path: str = ""
    # True when the source posting's amount was inferred by beancount (the
    # leg had no explicit number in the file). Used to recognise cross-bank
    # transit entries — e.g., the `Assets:B:PayPal` leg of an SPK→PayPal
    # transfer is inferred, while a real PayPal-CSV-derived entry has it
    # written explicitly.
    amount_inferred: bool = False
    # Alternate dates extracted from posting-level metadata (`actual:`,
    # `paypal:`, `settle:`, …). The user's plugins move postings to these
    # dates; matching against the original CSV row should consider both.
    metadata_dates: tuple[date, ...] = ()
    # True when the parent transaction has more than two postings — e.g.,
    # a salary entry with deduction legs. Diff logic suppresses category
    # changes against such entries because rewriting the merchant-side
    # account would corrupt a multi-posting structure the user authored.
    has_multiple_postings: bool = False


class ProposedChange(NamedTuple):
    field: str
    old_val: str
    new_val: str


class Posting(BaseModel):
    """One leg of a transaction beyond the source-account leg.

    `amount=None` produces a beancount-inferred (balancing) leg.
    `currency=None` inherits the source transaction's currency.
    """

    model_config = ConfigDict(frozen=True)

    account: str
    amount: Decimal | None = None
    currency: str | None = None
    metadata: dict[str, str] = {}


class CategoryProposal(BaseModel):
    # `CategorizationRule` is a forward reference; `arbitrary_types_allowed` is
    # not needed because the actual value is still a Pydantic BaseModel at runtime.
    model_config = ConfigDict(frozen=True)

    action: Literal["categorize", "skip", "quit"]
    postings: tuple[Posting, ...] = ()
    payee: str | None = None
    narration: str | None = None
    metadata: dict[str, str] = {}
    tag: str | None = None
    rule_used: CategorizationRule | None = None
    save_as_rule: bool = False
    # Optional tag-state mutation: set/clear the active tag mid-run (Screen
    # 1's `[t]` hotkey). Applied by the pipeline before `tag` stamping, so
    # picking "set always" on this screen tags this txn AND all that follow.
    tag_state_delta: TagStateDelta | None = None

    @property
    def target_account(self) -> str:
        """Convenience for the common single-posting case; '' if no postings."""
        return self.postings[0].account if self.postings else ""


class ImportResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_txn: SourceTransaction
    action: Literal["new", "update", "skip", "transfer", "quit"]
    matched_entry: LedgerEntry | None = None
    proposed_changes: list[ProposedChange] = []
    new_entry_text: str = ""
    # The proposal that produced this result. None for "skip"/"quit" without
    # a categorize call. The replay log records it verbatim.
    proposal: CategoryProposal | None = None
    rule_matched: CategorizationRule | None = None
    is_replay: bool = False
    new_rule: CategorizationRule | None = None
    tag_state_delta: TagStateDelta | None = None
    skip_reason: (
        Literal[
            "duplicate",
            "skip_rule",
            "cross_source_match",
            "user_kept",      # Screen 3 [k]: existing entry left as-is
            "user_skipped",   # Screen 3 [s]: row deferred to a later run
            "user_blocked",   # Screen 3 [b]: future updates from this payee blocked
        ]
        | None
    ) = None
