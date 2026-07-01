from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from beancount_importer.models import SourceTransaction


class CategorizationRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    target_account: str
    payee_pattern: str = ""
    description_pattern: str = ""
    # How `payee_pattern` / `description_pattern` are matched:
    #   "contains" — case-insensitive literal substring (patterns stay human
    #                readable; regex metacharacters are matched verbatim)
    #   "exact"    — case-insensitive whole-string equality
    #   "regex"    — `re.search(…, IGNORECASE)`
    # Defaults to "regex" so rules persisted before this field existed keep
    # their original semantics on load. New / derived rules use "contains".
    match_mode: Literal["contains", "exact", "regex"] = "regex"
    # When both patterns are set: False → both must match (AND, default);
    # True → either may match (OR). Lets one rule cover "payee OR narration"
    # instead of a duplicated payee-rule + description-rule pair.
    match_any: bool = False
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

    @model_validator(mode="after")
    def validate_regex(self) -> CategorizationRule:
        # Only regex-mode patterns must be valid regex; a "contains"/"exact"
        # pattern is a literal that may legitimately contain `[`, `*`, `(`, …
        if self.match_mode == "regex":
            for pattern in (self.payee_pattern, self.description_pattern):
                if pattern:
                    try:
                        re.compile(pattern, re.IGNORECASE)
                    except re.error as e:
                        raise ValueError(
                            f"Invalid regex pattern {pattern!r}: {e}"
                        ) from e
        return self

    def _pattern_matches(self, pattern: str, haystack: str) -> bool:
        if self.match_mode == "regex":
            return re.search(pattern, haystack, re.IGNORECASE) is not None
        h, p = haystack.casefold(), pattern.casefold()
        return h == p if self.match_mode == "exact" else p in h

    def matches(self, txn: SourceTransaction) -> bool:
        if self.bank_key and txn.bank_key != self.bank_key:
            return False

        if self.amount_sign == "credit" and txn.amount <= Decimal(0):
            return False
        if self.amount_sign == "debit" and txn.amount >= Decimal(0):
            return False

        results: list[bool] = []
        if self.payee_pattern:
            results.append(self._pattern_matches(self.payee_pattern, txn.payee or ""))
        if self.description_pattern:
            results.append(
                self._pattern_matches(self.description_pattern, txn.description or "")
            )
        if not results:
            return True
        return any(results) if self.match_any else all(results)
