"""In-place migration from the legacy vibe-coded importer.

The legacy script (`import_transactions.py`) lives next to the user's ledger,
rules, and CSV exports. We don't move any of that — `migrate_legacy()` writes
all new files into a single dotted directory so the project root stays clean:

  finances/
    import_transactions.py        (legacy — left in place)
    .import_config.json           (legacy rules — left in place, read for input)
    transactions/                 (untouched)
    documents/                    (untouched)
    .beancount-importer/          ← NEW
        config.toml               ← NEW
        rules.json                ← NEW (ported from .import_config.json)
        tag_state.json            ← NEW (active_tag + recent_tags ported)

Idempotent: any new file that already exists is left alone (an empty
`rules.json` from a prior failed migration is treated as absent).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import tomli_w
from rich.console import Console

from beancount_importer.rules.models import CategorizationRule
from beancount_importer.rules.storage import save_rules


# Mirrors the legacy `BANK_ACCOUNTS` map — duplicated here so the migrator
# doesn't need the legacy script on PYTHONPATH.
_LEGACY_BANK_ACCOUNTS: dict[str, str] = {
    "spk": "Assets:B:SPK",
    "n26": "Assets:B:N26",
    "paypal": "Assets:B:PayPal",
    "zinia": "Liabilities:CreditCard:Zinia",
    "cash": "Assets:Cash",
}

# CSV mappings reverse-engineered from the legacy parsers in
# `import_transactions.py` (SparkasseParser, N26Parser, PayPalParser,
# CashCsvParser). These reflect the actual headers each bank ships, so the
# `GenericCsvParser` can replace each legacy parser without further tweaking
# in the common case.
_LEGACY_CSV_DEFAULTS: dict[str, dict[str, Any]] = {
    "spk": {
        "delimiter": ";",
        # utf-8-sig handles the BOM Sparkasse exports often carry.
        "encoding": "utf-8-sig",
        "date_format": ["%d.%m.%y", "%d.%m.%Y"],
        "amount_locale": "de",
        "field_date": "Buchungstag",
        "field_value_date": "Valutadatum",
        "field_amount": "Betrag",
        "field_currency": "Waehrung",
        "field_payee": "Beguenstigter/Zahlungspflichtiger",
        "field_description": ["Verwendungszweck", "Buchungstext"],
        "field_sepa_reference": "Kundenreferenz (End-to-End)",
    },
    "n26": {
        "delimiter": ",",
        "encoding": "utf-8",
        "date_format": ["%Y-%m-%d"],
        "amount_locale": "en",
        "field_date": "Booking Date",
        "field_value_date": "Value Date",
        "field_amount": "Amount (EUR)",
        "field_payee": "Partner Name",
        "field_description": ["Payment Reference", "Type"],
        "field_original_amount": "Original Amount",
        "field_original_currency": "Original Currency",
    },
    "paypal": {
        "delimiter": ",",
        "encoding": "utf-8",
        # PayPal honors the locale of the account: German users get
        # `DD.MM.YYYY`, very old exports sometimes ship `MM/DD/YYYY`. Try
        # German first since that matches the modern de-DE exports — the
        # legacy importer used `parse_german_date` for the same reason.
        "date_format": ["%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"],
        # German PayPal accounts use comma decimals (`-7,49`) — same reason
        # we default to %d.%m.%Y above.
        "amount_locale": "de",
        "field_date": "Date",
        "field_amount": "Net",
        "field_currency": "Currency",
        "field_payee": "Name",
        "field_description": ["Description", "Subject"],
        "field_sepa_reference": "Transaction ID",
        # PayPal ships informational rows (auth holds, reversals) marked Memo;
        # they don't move money, so the importer should skip them.
        "skip_row_where": {"Balance Impact": "Memo"},
    },
    "cash": {
        "delimiter": ",",
        "encoding": "utf-8",
        "date_format": ["%Y-%m-%d"],
        "amount_locale": "en",
        "field_date": "date",
        "field_amount": "amount",
        "field_description": "description",
    },
    "zinia": {
        # Zinia (Amazon Visa) exports are .xls workbooks with a multi-row
        # preamble — `_read_xls_rows` auto-locates the header. delimiter and
        # encoding are unused on the xls path but the schema still requires
        # them.
        "delimiter": ",",
        "encoding": "utf-8",
        "date_format": ["%d.%m.%Y"],
        "amount_locale": "de",
        "field_date": "Datum",
        "field_amount": "Betrag",
        "field_payee": "Beschreibung",
        "field_description": ["Umsatzkategorie", "Unterkategorie"],
    },
}

_LEGACY_FILE_GLOBS: dict[str, str] = {
    # `**/` recurses through year-subdirs (`documents/2024/SPK_*.CSV` etc.)
    # since the legacy importer organized CSV exports per year.
    "spk": "../documents/**/SPK_*.CSV",
    # N26 exports through history have been both `N26_<year>.csv` and the
    # older `n26-<year>.csv` shape — case_sensitive=False at glob time means
    # `N26_*.csv` matches both casings, but we still need the underscore form
    # to match the modern exports the user actually has on disk.
    "n26": "../documents/**/N26_*.csv",
    "paypal": "../documents/**/PayPal_*.csv",
    "cash": "../documents/cash.csv",
    "zinia": "../documents/**/Zinia_*.xls",
}


# ── Public API ───────────────────────────────────────────────────────────────


def migrate_legacy(project_dir: Path, *, console: Console | None = None) -> None:
    """Migrate a legacy importer setup *in place*.

    Reads `.import_config.json` from `project_dir` and writes:
    - `import_config.toml`
    - `categorization_rules.json`
    - `.import-state/tag_state.json`

    Existing non-empty files are left untouched; an empty `categorization_rules.json`
    (e.g. left behind by an earlier failed migration) is overwritten — that's
    not user content worth preserving.
    """
    out = console if console is not None else Console()
    project_dir = project_dir.resolve()
    if not project_dir.exists():
        raise FileNotFoundError(project_dir)

    legacy_rules = project_dir / ".import_config.json"
    config_dir = project_dir / ".beancount-importer"
    config_path = config_dir / "config.toml"
    rules_path = config_dir / "rules.json"
    tags_path = config_dir / "tag_state.json"
    config_dir.mkdir(parents=True, exist_ok=True)

    def _show(p: Path) -> str:
        return str(p.relative_to(project_dir))

    if config_path.exists():
        out.print(f"[yellow]exists[/]: {_show(config_path)} (left untouched)")
    else:
        banks_seen = _detect_active_banks(project_dir)
        skip_patterns = _extract_legacy_skip_patterns(legacy_rules)
        toml_data = _build_config_toml(banks_seen, skip_patterns=skip_patterns)
        config_path.write_bytes(tomli_w.dumps(toml_data).encode("utf-8"))
        out.print(
            f"[green]wrote[/] {_show(config_path)} "
            f"({len(toml_data['banks'])} bank(s), {len(skip_patterns)} skip pattern(s))"
        )

    if _has_meaningful_rules(rules_path):
        out.print(f"[yellow]exists[/]: {_show(rules_path)} (left untouched)")
    else:
        rules = _convert_legacy_rules(legacy_rules)
        rules = _apply_legacy_suppress_lists(rules, _legacy_suppress_lists(legacy_rules))
        save_rules(rules, rules_path)
        out.print(f"[green]wrote[/] {_show(rules_path)} ({len(rules)} rule(s))")

    if tags_path.exists():
        out.print(f"[yellow]exists[/]: {_show(tags_path)} (left untouched)")
    else:
        tag_state = _convert_legacy_tag_state(legacy_rules)
        if tag_state is not None:
            tags_path.write_text(
                json.dumps(tag_state, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            recent_count = len(tag_state.get("recent", []))
            active = "active" if tag_state.get("active") else "no active"
            out.print(
                f"[green]wrote[/] {_show(tags_path)} ({active}, {recent_count} recent)"
            )


def _has_meaningful_rules(path: Path) -> bool:
    """Return True iff the file exists and parses as a non-empty JSON list.

    An empty `[]` is treated as absent so a previously-failed migration can be
    re-run without manual cleanup.
    """
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return True  # don't clobber unparseable user content
    return isinstance(data, list) and len(data) > 0


# ── Bank detection ──────────────────────────────────────────────────────────


def _detect_active_banks(project_dir: Path) -> list[str]:
    """Return the legacy bank keys we have evidence the user actually uses.

    Two signals: (a) at least one CSV in `documents/` matches the bank's
    file_glob, or (b) a `.bean` file named after the bank already exists in
    `transactions/`. Either is enough — the legacy script worked off both.
    Falls back to all known banks if nothing matches, so a fresh-install user
    still gets a useful starter.
    """
    seen: list[str] = []
    docs = project_dir / "documents"
    txdir = project_dir / "transactions"

    for key in _LEGACY_BANK_ACCOUNTS:
        glob = _LEGACY_FILE_GLOBS.get(key, f"{key}_*.csv")
        has_csv = bool(list(docs.glob(f"**/{glob}"))) if docs.exists() else False
        has_bean = False
        if txdir.exists():
            has_bean = bool(list(txdir.glob(f"**/{key.upper()}.bean")))
        if has_csv or has_bean:
            seen.append(key)

    return seen or list(_LEGACY_BANK_ACCOUNTS)


# ── Config synthesis ─────────────────────────────────────────────────────────


def _apply_legacy_suppress_lists(
    rules: list[CategorizationRule],
    suppress_lists: dict[str, list[str]],
) -> list[CategorizationRule]:
    """Set per-rule suppress flags based on the legacy global suppress lists.

    The legacy script stored four lists of pattern strings; if a rule's pattern
    appeared in one, it got the corresponding suppression flag. We replicate
    that by case-insensitively matching the new rule's payee_pattern *or*
    description_pattern against each list.
    """
    if not any(suppress_lists.values()):
        return rules

    def _hits(rule: CategorizationRule, patterns: list[str]) -> bool:
        haystack = (rule.payee_pattern or rule.description_pattern or "").lower()
        return any(p and p.lower() in haystack for p in patterns)

    out: list[CategorizationRule] = []
    for rule in rules:
        updates: dict[str, Any] = {}
        for flag, patterns in suppress_lists.items():
            if _hits(rule, patterns):
                updates[flag] = True
        out.append(rule.model_copy(update=updates) if updates else rule)
    return out


def _build_config_toml(
    banks_seen: list[str],
    *,
    skip_patterns: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    banks: list[dict[str, Any]] = []
    for key in banks_seen:
        csv_defaults = _LEGACY_CSV_DEFAULTS.get(key)
        if csv_defaults is None:
            continue
        banks.append({
            "key": key,
            "display_name": key.upper(),
            "account": _LEGACY_BANK_ACCOUNTS[key],
            "file_glob": _LEGACY_FILE_GLOBS.get(key, f"{key}_*.csv"),
            # Paths in the TOML are resolved relative to the config file's
            # directory (`.beancount-importer/`), so transactions/ and
            # documents/ are referenced as siblings via `..`.
            "output_file": f"../transactions/{{year}}/{key.upper()}.bean",
            "csv": dict(csv_defaults),
        })

    config: dict[str, Any] = {
        "rules_file": "rules.json",
        "decisions_file": "decisions.jsonl",
        "tag_state_file": "tag_state.json",
        "documents_dir": "../documents",
        "transactions_dir": "../transactions",
        "matching": {
            "min_score": 0.35,
            "min_delta": 0.15,
            "transfer_tolerance_days": 5,
            "transit_account": "Assets:Extern:Transit",
            "internal_transfer_account_prefixes": [
                "Assets:B:",
                "Liabilities:CreditCard:",
            ],
        },
        "banks": banks,
    }
    if skip_patterns:
        config["skip_update_patterns"] = skip_patterns
    return config


# ── Rules conversion ─────────────────────────────────────────────────────────


def _convert_legacy_rules(rules_json: Path) -> list[CategorizationRule]:
    """Convert per-rule entries from `.import_config.json` to `CategorizationRule`s.

    The legacy schema looks like:
        {"pattern": "...", "match_field": "payee" | "any",
         "target_account": "...", "source_bank": "spk",
         "amount_sign": "negative" | "",
         "default_payee": ..., "default_description": ...,
         "add_settle_days": int|null,
         "amortize_type": ..., "amortize_months": int|null}

    `match_field="any"` had OR semantics across payee/narration/description;
    the new model is AND, so we expand `any` into TWO rules — one keyed on
    payee, one on description — both pointing at the same target.
    """
    if not rules_json.exists():
        return []
    try:
        raw = json.loads(rules_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    items = raw.get("rules") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []

    out: list[CategorizationRule] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            out.extend(_legacy_rule_to_new(item))
        except (KeyError, ValueError):
            continue
    return out


_LEGACY_AMOUNT_SIGN_MAP: dict[str, str] = {
    "negative": "debit",
    "positive": "credit",
    "debit": "debit",
    "credit": "credit",
    "": "",
}


def _legacy_rule_to_new(item: dict[str, Any]) -> list[CategorizationRule]:
    target = item.get("target_account") or item.get("category")
    if not target:
        return []
    pattern = item.get("pattern") or item.get("payee_pattern") or ""
    match_field = (item.get("match_field") or "payee").lower()

    # Legacy "negative"/"positive" → new "debit"/"credit".
    sign_legacy = (item.get("amount_sign") or "").lower()
    amount_sign = _LEGACY_AMOUNT_SIGN_MAP.get(sign_legacy, "")

    base: dict[str, Any] = {
        "target_account": target,
        "amount_sign": amount_sign,
        "bank_key": (item.get("source_bank") or "").strip(),
        "override_payee": item.get("default_payee") or None,
        "override_narration": item.get("default_description") or None,
        # Transform fields — legacy used `add_settle_days` but stored an int.
        "settle_days": item.get("add_settle_days") or item.get("settle_days"),
        "amortize_months": item.get("amortize_months"),
        "amortize_type": item.get("amortize_type") or "",
    }

    # `match_field="any"` had OR semantics — expand to two rules sharing a
    # target so either payee OR description match still routes correctly.
    if match_field == "any" and pattern:
        return [
            CategorizationRule.model_validate({**base, "payee_pattern": pattern}),
            CategorizationRule.model_validate({**base, "description_pattern": pattern}),
        ]

    # Default: payee match.
    if not pattern:
        # Without a pattern the rule would match every transaction — drop it.
        return []
    field_key = "description_pattern" if match_field == "description" else "payee_pattern"
    return [CategorizationRule.model_validate({**base, field_key: pattern})]


# ── Tag state conversion ─────────────────────────────────────────────────────


def _convert_legacy_tag_state(rules_json: Path) -> dict[str, Any] | None:
    """Pull `active_tag` + `recent_tags` from the legacy config.

    The legacy script wrote them either at the top level of `.import_config.json`
    or nested under a `config` block (the more recent layout). We accept both —
    nested takes precedence since it's the format the active script writes.
    """
    if not rules_json.exists():
        return None
    try:
        raw = json.loads(rules_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None

    nested_raw = raw.get("config")
    nested: dict[str, Any] = nested_raw if isinstance(nested_raw, dict) else {}
    active_data = nested.get("active_tag") if "active_tag" in nested else raw.get("active_tag")
    recent = nested.get("recent_tags") if "recent_tags" in nested else (raw.get("recent_tags") or [])

    if not active_data and not recent:
        return None

    active_payload: dict[str, Any] | None = None
    if isinstance(active_data, dict) and active_data.get("tag"):
        mode = active_data.get("mode", "always")
        if mode not in ("always", "once", "duration"):
            mode = "always"
        active_payload = {
            "tag": active_data["tag"],
            "mode": mode,
            "from_date": active_data.get("from_date"),
            "until_date": active_data.get("until_date"),
        }

    return {
        "active": active_payload,
        "recent": list(recent) if isinstance(recent, list) else [],
    }


def _extract_legacy_skip_patterns(rules_json: Path) -> list[dict[str, str]]:
    """Convert the legacy `skip_update_rules` list into `SkipUpdatePattern` entries.

    Legacy entries look like `{"pattern": "...", "match_field": "narration|payee|any|exact"}`.
    `exact` matched on a synthetic key built from narration+date+amount; the new
    model doesn't have that mode, so those entries are dropped. `any` expands
    to a payee pattern *and* a narration pattern (OR semantics, like rule conversion).
    """
    if not rules_json.exists():
        return []
    try:
        raw = json.loads(rules_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, dict):
        return []
    nested_raw = raw.get("config")
    nested: dict[str, Any] = nested_raw if isinstance(nested_raw, dict) else raw
    legacy = nested.get("skip_update_rules") or []
    if not isinstance(legacy, list):
        return []

    out: list[dict[str, str]] = []
    for item in legacy:
        if not isinstance(item, dict):
            continue
        pattern = item.get("pattern")
        if not pattern:
            continue
        match_field = (item.get("match_field") or "").lower()
        if match_field == "exact":
            # No equivalent in the new model — these are one-off date+amount
            # locks that the decision log handles instead.
            continue
        if match_field == "any":
            out.append({"field": "payee", "pattern": pattern})
            out.append({"field": "narration", "pattern": pattern})
        elif match_field in ("payee", "narration", "description"):
            out.append({"field": match_field, "pattern": pattern})
        else:
            # Default to narration if unknown — matches legacy fall-through.
            out.append({"field": "narration", "pattern": pattern})
    return out


def _legacy_suppress_lists(rules_json: Path) -> dict[str, list[str]]:
    """Return the four legacy suppress lists keyed by their new-model flag name.

    These are global pattern lists ("if a rule's *pattern* matches one of
    these strings, set suppress_X=True on that rule"). Returned as raw lists
    so the caller can apply them after rule conversion.
    """
    if not rules_json.exists():
        return {}
    try:
        raw = json.loads(rules_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    cfg_raw = raw.get("config")
    cfg: dict[str, Any] = cfg_raw if isinstance(cfg_raw, dict) else raw
    return {
        "suppress_updates": list(cfg.get("suppress_updates_for_rules") or []),
        "suppress_narration_updates": list(cfg.get("suppress_narration_updates_for_rules") or []),
        "suppress_payee_updates": list(cfg.get("suppress_payee_updates_for_rules") or []),
        "suppress_account_updates": list(cfg.get("suppress_category_updates_for_rules") or []),
    }


# ── Year-dir scaffolding ─────────────────────────────────────────────────────


def ensure_year_dir(transactions_dir: Path, year: int) -> Path:
    """Create `transactions/{year}/` and return its path."""
    year_dir = transactions_dir / str(year)
    year_dir.mkdir(parents=True, exist_ok=True)
    return year_dir
