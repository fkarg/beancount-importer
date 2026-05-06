"""Tests for the in-place legacy migrator + year-dir helper.

The migrator is exercised through its observable file-system effects rather
than internal helpers, so the per-bank defaults and detection rules can be
refined without touching tests.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from beancount_importer.scaffolding import ensure_year_dir, migrate_legacy


def _seed_legacy(
    project_dir: Path,
    *,
    rules: list[dict] | None = None,
    active_tag: dict | None = None,
    recent_tags: list[str] | None = None,
    csv_files: list[str] | None = None,
    bean_files: list[tuple[str, str]] | None = None,
) -> None:
    """Materialize a fake legacy project tree for testing.

    `csv_files` lists CSV names dropped under `documents/`. `bean_files` is a
    list of `(year, bank_key)` pairs used to create per-bank ledger files; the
    migrator's bank-detection logic looks at both.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    legacy_config: dict = {"rules": rules or []}
    if active_tag is not None:
        legacy_config["active_tag"] = active_tag
    if recent_tags is not None:
        legacy_config["recent_tags"] = recent_tags
    (project_dir / ".import_config.json").write_text(json.dumps(legacy_config))

    docs = project_dir / "documents"
    docs.mkdir(exist_ok=True)
    for name in csv_files or []:
        (docs / name).write_text("placeholder\n")

    txdir = project_dir / "transactions"
    txdir.mkdir(exist_ok=True)
    for year, bank in bean_files or []:
        year_dir = txdir / year
        year_dir.mkdir(exist_ok=True)
        (year_dir / f"{bank.upper()}.bean").write_text("; placeholder\n")


class TestMigrateLegacyInPlace:
    def test_writes_three_files_alongside_legacy(self, tmp_path: Path):
        _seed_legacy(tmp_path, csv_files=["SPK_2024.CSV"])
        migrate_legacy(tmp_path)
        assert (tmp_path / ".beancount-importer" / "config.toml").exists()
        assert (tmp_path / ".beancount-importer" / "rules.json").exists()
        # No active tag / recent tags supplied → no tag-state file written
        assert not (tmp_path / ".beancount-importer" / "tag_state.json").exists()
        # Legacy file survives
        assert (tmp_path / ".import_config.json").exists()

    def test_emits_only_detected_banks(self, tmp_path: Path):
        # Only an SPK CSV present → only an SPK bank section emitted.
        _seed_legacy(tmp_path, csv_files=["SPK_2024.CSV"])
        migrate_legacy(tmp_path)
        cfg = tomllib.loads((tmp_path / ".beancount-importer" / "config.toml").read_text())
        keys = {b["key"] for b in cfg["banks"]}
        assert keys == {"spk"}

    def test_detects_via_bean_file(self, tmp_path: Path):
        _seed_legacy(tmp_path, bean_files=[("2024", "n26")])
        migrate_legacy(tmp_path)
        cfg = tomllib.loads((tmp_path / ".beancount-importer" / "config.toml").read_text())
        keys = {b["key"] for b in cfg["banks"]}
        assert "n26" in keys

    def test_falls_back_to_all_when_nothing_detected(self, tmp_path: Path):
        _seed_legacy(tmp_path)
        migrate_legacy(tmp_path)
        cfg = tomllib.loads((tmp_path / ".beancount-importer" / "config.toml").read_text())
        keys = {b["key"] for b in cfg["banks"]}
        # All known banks emitted as a starter
        assert {"spk", "n26", "paypal", "cash"}.issubset(keys)

    def test_spk_csv_defaults_match_legacy_parser(self, tmp_path: Path):
        _seed_legacy(tmp_path, csv_files=["SPK_2024.CSV"])
        migrate_legacy(tmp_path)
        cfg = tomllib.loads((tmp_path / ".beancount-importer" / "config.toml").read_text())
        spk = next(b for b in cfg["banks"] if b["key"] == "spk")
        assert spk["csv"]["delimiter"] == ";"
        assert spk["csv"]["amount_locale"] == "de"
        assert spk["csv"]["field_date"] == "Buchungstag"
        assert spk["csv"]["field_sepa_reference"] == "Kundenreferenz (End-to-End)"

    def test_paypal_skips_memo_rows(self, tmp_path: Path):
        _seed_legacy(tmp_path, csv_files=["PayPal_2024.csv"])
        migrate_legacy(tmp_path)
        cfg = tomllib.loads((tmp_path / ".beancount-importer" / "config.toml").read_text())
        pp = next(b for b in cfg["banks"] if b["key"] == "paypal")
        assert pp["csv"]["skip_row_where"] == {"Balance Impact": "Memo"}

    def test_converts_simple_rule(self, tmp_path: Path):
        _seed_legacy(
            tmp_path,
            rules=[{"target_account": "Expenses:Streaming", "payee_pattern": "Netflix"}],
        )
        migrate_legacy(tmp_path)
        rules = json.loads((tmp_path / ".beancount-importer" / "rules.json").read_text())
        assert len(rules) == 1
        assert rules[0]["target_account"] == "Expenses:Streaming"
        assert rules[0]["payee_pattern"] == "Netflix"

    def test_converts_legacy_pattern_match_field_schema(self, tmp_path: Path):
        # The actual on-disk legacy schema — `pattern` + `match_field`,
        # `default_payee`, `source_bank`, `amount_sign="negative"`. This is
        # what `~/finances/.import_config.json` looks like.
        _seed_legacy(
            tmp_path,
            rules=[{
                "pattern": "PENNY",
                "target_account": "Expenses:Food:Groceries",
                "match_field": "payee",
                "source_bank": "spk",
                "default_payee": "Penny",
                "amount_sign": "negative",
            }],
        )
        migrate_legacy(tmp_path)
        rules = json.loads((tmp_path / ".beancount-importer" / "rules.json").read_text())
        assert len(rules) == 1
        r = rules[0]
        assert r["payee_pattern"] == "PENNY"
        assert r["target_account"] == "Expenses:Food:Groceries"
        assert r["bank_key"] == "spk"
        assert r["override_payee"] == "Penny"
        # Legacy "negative" maps to new model's "debit"
        assert r["amount_sign"] == "debit"

    def test_match_field_any_expands_to_two_rules(self, tmp_path: Path):
        _seed_legacy(
            tmp_path,
            rules=[{
                "pattern": "Bahn",
                "target_account": "Expenses:Transport",
                "match_field": "any",
            }],
        )
        migrate_legacy(tmp_path)
        rules = json.loads((tmp_path / ".beancount-importer" / "rules.json").read_text())
        assert len(rules) == 2
        # Same target, but one rule keys on payee, the other on description
        targets = {r["target_account"] for r in rules}
        assert targets == {"Expenses:Transport"}
        keys = {("payee" if r["payee_pattern"] else "description") for r in rules}
        assert keys == {"payee", "description"}

    def test_re_runs_on_empty_rules_file(self, tmp_path: Path):
        # Simulates the bug: an earlier broken migration left an empty `[]`.
        # Re-running should populate it from the legacy file.
        _seed_legacy(
            tmp_path,
            rules=[{"pattern": "X", "target_account": "Expenses:X", "match_field": "payee"}],
        )
        config_dir = tmp_path / ".beancount-importer"
        config_dir.mkdir()
        (config_dir / "rules.json").write_text("[]")
        migrate_legacy(tmp_path)
        rules = json.loads((config_dir / "rules.json").read_text())
        assert len(rules) == 1

    def test_skip_update_rules_become_skip_patterns(self, tmp_path: Path):
        legacy = {
            "rules": [],
            "config": {
                "skip_update_rules": [
                    {"pattern": "Train Ticket", "match_field": "narration"},
                    {"pattern": "Some Date Anchor", "match_field": "exact"},  # dropped
                ],
            },
        }
        (tmp_path / ".import_config.json").write_text(json.dumps(legacy))
        migrate_legacy(tmp_path)
        cfg = tomllib.loads((tmp_path / ".beancount-importer" / "config.toml").read_text())
        skip = cfg.get("skip_update_patterns", [])
        assert len(skip) == 1
        assert skip[0]["field"] == "narration"
        assert skip[0]["pattern"] == "Train Ticket"

    def test_global_suppress_lists_set_per_rule_flags(self, tmp_path: Path):
        legacy = {
            "rules": [
                {"pattern": "www.steampowered.com", "target_account": "Expenses:Games", "match_field": "payee"},
                {"pattern": "Other", "target_account": "Expenses:Other", "match_field": "payee"},
            ],
            "config": {
                "suppress_narration_updates_for_rules": ["www.steampowered.com"],
            },
        }
        (tmp_path / ".import_config.json").write_text(json.dumps(legacy))
        migrate_legacy(tmp_path)
        rules = json.loads((tmp_path / ".beancount-importer" / "rules.json").read_text())
        steam = next(r for r in rules if r["payee_pattern"] == "www.steampowered.com")
        other = next(r for r in rules if r["payee_pattern"] == "Other")
        assert steam["suppress_narration_updates"] is True
        assert other["suppress_narration_updates"] is False

    def test_active_tag_under_config_block(self, tmp_path: Path):
        # The actual layout: active_tag + recent_tags nested under "config".
        legacy = {
            "rules": [],
            "config": {
                "active_tag": {"tag": "trip-de", "mode": "duration",
                               "from_date": "2024-06-01", "until_date": "2024-06-15"},
                "recent_tags": ["trip-de", "lunch"],
            },
        }
        (tmp_path / ".import_config.json").write_text(json.dumps(legacy))
        migrate_legacy(tmp_path)
        ts = json.loads((tmp_path / ".beancount-importer" / "tag_state.json").read_text())
        assert ts["active"]["tag"] == "trip-de"
        assert ts["active"]["mode"] == "duration"
        assert ts["recent"] == ["trip-de", "lunch"]

    def test_skips_rule_without_target(self, tmp_path: Path):
        _seed_legacy(
            tmp_path,
            rules=[
                {"payee_pattern": "Netflix"},  # no target → dropped
                {"target_account": "Expenses:X", "payee_pattern": "X"},
            ],
        )
        migrate_legacy(tmp_path)
        rules = json.loads((tmp_path / ".beancount-importer" / "rules.json").read_text())
        assert len(rules) == 1
        assert rules[0]["target_account"] == "Expenses:X"

    def test_ports_active_tag_and_recent(self, tmp_path: Path):
        _seed_legacy(
            tmp_path,
            active_tag={"tag": "trip-berlin", "mode": "always"},
            recent_tags=["trip-berlin", "lunch"],
        )
        migrate_legacy(tmp_path)
        ts = json.loads((tmp_path / ".beancount-importer" / "tag_state.json").read_text())
        assert ts["active"]["tag"] == "trip-berlin"
        assert ts["active"]["mode"] == "always"
        assert ts["recent"] == ["trip-berlin", "lunch"]

    def test_idempotent_does_not_overwrite(self, tmp_path: Path):
        _seed_legacy(tmp_path, rules=[])
        config_dir = tmp_path / ".beancount-importer"
        config_dir.mkdir()
        custom = "# my hand-edited config\n"
        (config_dir / "config.toml").write_text(custom)
        migrate_legacy(tmp_path)
        assert (config_dir / "config.toml").read_text() == custom

    def test_does_not_touch_existing_rules(self, tmp_path: Path):
        _seed_legacy(
            tmp_path,
            rules=[{"pattern": "X", "target_account": "Expenses:Legacy", "match_field": "payee"}],
        )
        config_dir = tmp_path / ".beancount-importer"
        config_dir.mkdir()
        existing = json.dumps([{"target_account": "Expenses:Manual"}])
        (config_dir / "rules.json").write_text(existing)
        migrate_legacy(tmp_path)
        kept = json.loads((config_dir / "rules.json").read_text())
        assert kept == [{"target_account": "Expenses:Manual"}]


class TestEnsureYearDir:
    def test_creates_directory(self, tmp_path: Path):
        out = ensure_year_dir(tmp_path / "transactions", 2024)
        assert out.is_dir()
        assert out.name == "2024"

    def test_idempotent(self, tmp_path: Path):
        d = tmp_path / "transactions"
        ensure_year_dir(d, 2024)
        result = ensure_year_dir(d, 2024)
        assert result.is_dir()
