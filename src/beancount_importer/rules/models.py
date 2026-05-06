from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from beancount_importer.models import SourceTransaction


class CategorizationRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    target_account: str
    payee_pattern: str = ""
    description_pattern: str = ""
    # "credit" = positive amounts, "debit" = negative, "" = any
    amount_sign: Literal["credit", "debit", ""] = ""
    bank_key: str = ""  # empty = match any bank
    override_payee: str | None = None
    override_narration: str | None = None
    tag: str | None = None

    # ── Suppression flags ─────────────────────────────────────────────────
    # When the rule matches an *existing* LedgerEntry, these decide whether
    # the proposed-changes list is filtered or skipped entirely.
    suppress_updates: bool = False
    suppress_payee_updates: bool = False
    suppress_narration_updates: bool = False
    suppress_account_updates: bool = False

    # ── Transform inputs ──────────────────────────────────────────────────
    # Hooks in `transforms/` consume these to generate metadata.
    settle_days: int | None = None       # `settle:` metadata; offset from booking
    add_actual_date: bool = False        # `actual:` metadata; card-swipe vs booking
    amortize_months: int | None = None   # `amortize:` metadata; spread cost
    # Matches the three modes from the beancount-amortize plugin:
    #   "lifetime_months"  — depreciation over expected lifetime
    #   "prepaid_months"   — prepaid expense, recognized monthly
    #   "amortize_months"  — generic amortization with no intermediate asset
    amortize_type: Literal["", "lifetime_months", "prepaid_months", "amortize_months"] = ""

    @field_validator("payee_pattern", "description_pattern")
    @classmethod
    def validate_regex(cls, v: str) -> str:
        if v:
            try:
                re.compile(v, re.IGNORECASE)
            except re.error as e:
                raise ValueError(f"Invalid regex pattern {v!r}: {e}") from e
        return v

    def matches(self, txn: SourceTransaction) -> bool:
        if self.bank_key and txn.bank_key != self.bank_key:
            return False

        if self.amount_sign == "credit" and txn.amount <= Decimal(0):
            return False
        if self.amount_sign == "debit" and txn.amount >= Decimal(0):
            return False

        if self.payee_pattern:
            haystack = txn.payee or ""
            if not re.search(self.payee_pattern, haystack, re.IGNORECASE):
                return False

        if self.description_pattern:
            haystack = txn.description or ""
            if not re.search(self.description_pattern, haystack, re.IGNORECASE):
                return False

        return True
