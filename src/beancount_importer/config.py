from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class CsvConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    delimiter: str = ","
    encoding: str = "utf-8"
    date_format: list[str] = ["%Y-%m-%d"]
    amount_locale: str = "en"  # "de" for German (1.234,56), "en" for 1,234.56
    skip_zero_amounts: bool = False
    skip_row_where: dict[str, str] = {}

    # Required column mappings
    field_date: str
    field_amount: str

    # Optional column mappings
    field_value_date: str | None = None
    field_currency: str | None = None
    field_payee: str | None = None
    # Can be a single column name or a list (values joined with space)
    field_description: str | list[str] = []
    # Description parts equal to any string here (case-insensitive, after
    # trimming) are dropped before joining. Handles bank-specific noise
    # values like N26's `Type: Presentment` without bank-specific parser
    # code.
    field_description_blacklist: list[str] = []
    field_sepa_reference: str | None = None
    field_original_amount: str | None = None
    field_original_currency: str | None = None

    @field_validator("date_format", mode="before")
    @classmethod
    def coerce_date_format(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            return [v]
        return v

    @field_validator("field_description", mode="before")
    @classmethod
    def coerce_field_description(cls, v: Any) -> str | list[str]:
        if isinstance(v, str):
            return [v]
        return v


class BankConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    display_name: str
    account: str
    file_glob: str
    output_file: str
    source_files: list[str] = []
    csv: CsvConfig

    @model_validator(mode="before")
    @classmethod
    def default_source_files(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("source_files"):
            output = data.get("output_file")
            if output is not None:
                data = {**data, "source_files": [output]}
        return data


class SkipUpdatePattern(BaseModel):
    """Aggressive suppression: when this matches a SourceTransaction or its
    candidate LedgerEntry, the proposal is dropped entirely (no prompt).

    `field` chooses which haystack the regex runs against:
    - `payee` / `description`: SourceTransaction fields
    - `narration`: matched LedgerEntry.narration
    """

    model_config = ConfigDict(frozen=True)

    field: Literal["payee", "description", "narration"]
    pattern: str

    @field_validator("pattern")
    @classmethod
    def validate_regex(cls, v: str) -> str:
        try:
            re.compile(v, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern {v!r}: {e}") from e
        return v


class TransformsConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: list[str] = [
        "beancount_importer.transforms.settle",
        "beancount_importer.transforms.actual",
        "beancount_importer.transforms.amortize",
    ]


class MatchingConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    # Tightened post-audit: 0.5 paired with the Phase 2 diff suppressions
    # avoids both the old false-positive prompts (text drift under 0.35)
    # and the new false-negatives where a coincidental amount+text match
    # would otherwise propose an update.
    min_score: float = 0.5
    min_delta: float = 0.15
    # Maximum date gap for the scorer to admit a candidate. 7 days
    # comfortably covers the typical SPK→PayPal lag without straying into
    # "any same-amount row this month" territory.
    max_date_days: int = 7
    # Tighter window for `find_definitive_duplicate`. Strict dedup stays
    # narrower than the scoring window so accidental silent-skips across
    # week-plus distances can't happen.
    dedup_max_date_days: int = 5
    transfer_tolerance_days: int = 5
    transit_account: str = "Assets:Extern:Transit"
    internal_transfer_account_prefixes: list[str] = ["Assets:B:", "Liabilities:CreditCard:"]
    # Posting-level metadata keys whose value is parsed as an alternate
    # transaction date. Defaults match the user's `plugins/actual.py`,
    # `plugins/settle.py`, and `plugins/settle_inv.py` conventions; users
    # without those plugins can leave the defaults — the keys simply won't
    # appear in their ledgers and have no effect.
    metadata_date_keys: list[str] = ["actual", "paypal", "settle"]
    # Maps a posting-level metadata key to the account that the user's
    # plugin synthesizes a posting on at load time. With
    # `plugin "plugins.settle_inv" "Assets:B:PayPal"`, an SPK posting
    # carrying `paypal: 2024-01-17` is split — at runtime — into a separate
    # PayPal-side transaction on 2024-01-17. We don't run plugins, so the
    # importer reconstructs that virtual entry from the metadata to make
    # cross-bank matching work without the plugin loaded.
    synthesize_from_metadata: dict[str, str] = {}
    # Cross-source matchers run before user prompting: they spot duplicates
    # already booked in another bank's ledger (skip), or rewrite a proposal's
    # target account when a row is part of a known cross-source pair (e.g.,
    # PayPal-funded SPK debit → transfer to `Assets:B:PayPal`). Order matters;
    # the first matcher to fire wins. See `matching/registry.py`.
    enabled_matchers: list[str] = [
        "beancount_importer.matching.settled",
        "beancount_importer.matching.via_paypal",
        "beancount_importer.matching.transfers",
        "beancount_importer.matching.paypal",
    ]


class Config(BaseModel):
    model_config = ConfigDict(frozen=True)

    banks: list[BankConfig] = []
    # Everything below is resolved relative to the *finances root* — the
    # parent of `.beancount-importer/` in the standard layout (or whatever
    # the CLI computed via `--root` / CWD fallback). State files keep living
    # inside `.beancount-importer/`; the defaults spell out that prefix.
    rules_file: str = ".beancount-importer/rules.json"
    decisions_file: str = ".beancount-importer/decisions.jsonl"
    tag_state_file: str = ".beancount-importer/tag_state.json"
    documents_dir: str = "documents"
    transactions_dir: str = "transactions"
    # The "haven't decided yet" account. Decisions landing here are placeholders
    # and are never recorded to the replay log (nor would they be worth
    # replaying over a later rule).
    placeholder_account: str = "Expenses:Unknown"
    # When true, a successful (non-dry-run) import commits the files it owns
    # — rules, decisions, tag state, and the written ledger — in the finances
    # root. Opt-in; the `--commit/--no-commit` CLI flag overrides it per run.
    auto_commit_after_run: bool = False
    # File whose `open` directives define the authoritative account chart the
    # interactive pickers list. Resolved relative to the finances root. The
    # transaction sweep under `transactions_dir` only ever surfaces accounts
    # that appear on a posting, so accounts opened here but not yet used (or
    # only ever a 3rd+ posting leg) are invisible without this. If the file is
    # absent, the pipeline falls back to `main_bean`'s include-resolved opens.
    accounts_file: str = "accounts.bean"
    # Path to the top-level beancount file used by `bean-query` to compute
    # the plugin-expanded transaction count shown in the preview. May contain
    # `{year}` for per-year main files. Optional — the count is skipped if
    # the file does not exist or `bean-query` is not on PATH.
    main_bean: str | None = None
    skip_update_patterns: list[SkipUpdatePattern] = []
    # Max characters retained when writing the narration of new/updated
    # entries. Matches the reference's silent-truncation behaviour (no
    # ellipsis suffix); raise or lower per-project taste.
    narration_max_length: int = 70
    # Account name of the user's PayPal intermediary, if any. When a
    # cross-bank transfer's target equals this account the date-difference
    # metadata key becomes `paypal:` rather than `actual:` to match the
    # `plugins/settle_inv` convention.
    paypal_account: str | None = None
    transforms: TransformsConfig = TransformsConfig()
    matching: MatchingConfig = MatchingConfig()

    @classmethod
    def load(cls, path: Path) -> Config:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls.model_validate(data)

    def bank(self, key: str) -> BankConfig:
        for b in self.banks:
            if b.key == key:
                return b
        raise KeyError(f"No bank with key {key!r}")
