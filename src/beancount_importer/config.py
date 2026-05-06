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

    min_score: float = 0.35
    min_delta: float = 0.15
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


class Config(BaseModel):
    model_config = ConfigDict(frozen=True)

    banks: list[BankConfig] = []
    # Everything below is resolved relative to the config file's parent
    # directory. The CLI defaults that directory to `.beancount-importer/`,
    # so a project root has only that single dotted folder plus the user's
    # own `transactions/` and `documents/`.
    rules_file: str = "rules.json"
    decisions_file: str = "decisions.jsonl"
    tag_state_file: str = "tag_state.json"
    documents_dir: str = "../documents"
    transactions_dir: str = "../transactions"
    # Path to the top-level beancount file used by `bean-query` to compute
    # the plugin-expanded transaction count shown in the preview. May contain
    # `{year}` for per-year main files. Optional — the count is skipped if
    # the file does not exist or `bean-query` is not on PATH.
    main_bean: str | None = None
    skip_update_patterns: list[SkipUpdatePattern] = []
    transforms: TransformsConfig = TransformsConfig()
    matching: MatchingConfig = MatchingConfig()

    @classmethod
    def load(cls, path: Path) -> "Config":
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls.model_validate(data)

    def bank(self, key: str) -> BankConfig:
        for b in self.banks:
            if b.key == key:
                return b
        raise KeyError(f"No bank with key {key!r}")
