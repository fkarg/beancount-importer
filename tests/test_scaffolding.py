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


# ── Failure-mode coverage: the migrator never crashes on malformed legacy JSON
# ── and refuses to clobber pre-existing user content.


class TestMigrateLegacyDefensiveBranches:
    def test_missing_project_dir_raises(self, tmp_path: Path):
        import pytest
        with pytest.raises(FileNotFoundError):
            migrate_legacy(tmp_path / "does_not_exist")

    def test_existing_tag_state_left_untouched(self, tmp_path: Path):
        _seed_legacy(
            tmp_path,
            active_tag={"tag": "trip", "mode": "always"},
        )
        config_dir = tmp_path / ".beancount-importer"
        config_dir.mkdir()
        existing = '{"active": {"tag": "manual"}, "recent": []}'
        (config_dir / "tag_state.json").write_text(existing)
        migrate_legacy(tmp_path)
        # Migration must NOT clobber a pre-existing tag_state.json.
        assert (config_dir / "tag_state.json").read_text() == existing

    def test_unparseable_existing_rules_left_untouched(self, tmp_path: Path):
        # A rules.json that's truthy but unparseable must be treated as
        # user content (we don't know what they meant) — the migrator
        # leaves it in place rather than overwriting.
        _seed_legacy(
            tmp_path,
            rules=[{"pattern": "X", "target_account": "Expenses:X", "match_field": "payee"}],
        )
        config_dir = tmp_path / ".beancount-importer"
        config_dir.mkdir()
        garbage = "not valid json {{{"
        (config_dir / "rules.json").write_text(garbage)
        migrate_legacy(tmp_path)
        assert (config_dir / "rules.json").read_text() == garbage

    def test_no_legacy_file_writes_empty_rules(self, tmp_path: Path):
        # No `.import_config.json` at all — migration synthesizes empty rules
        # rather than failing. (`_convert_legacy_rules` returns [] when the
        # legacy path is absent.)
        tmp_path.mkdir(exist_ok=True)
        # Create the project dir without any legacy file.
        (tmp_path / "documents").mkdir()
        (tmp_path / "transactions").mkdir()
        migrate_legacy(tmp_path)
        rules = json.loads((tmp_path / ".beancount-importer" / "rules.json").read_text())
        assert rules == []

    def test_corrupt_legacy_rules_json_yields_empty(self, tmp_path: Path):
        # A `.import_config.json` that fails to parse should result in an
        # empty rules list (not a crash). Same for the tag-state extraction.
        tmp_path.mkdir(exist_ok=True)
        (tmp_path / ".import_config.json").write_text("{this is not json")
        migrate_legacy(tmp_path)
        rules = json.loads((tmp_path / ".beancount-importer" / "rules.json").read_text())
        assert rules == []
        # Corrupt JSON also can't yield a tag_state — file must NOT be created.
        assert not (tmp_path / ".beancount-importer" / "tag_state.json").exists()

    def test_legacy_top_level_is_list_not_dict(self, tmp_path: Path):
        # The legacy importer also accepted a bare list at the top level.
        # Tag-state extraction must short-circuit when it's not a dict.
        tmp_path.mkdir(exist_ok=True)
        (tmp_path / ".import_config.json").write_text(
            '[{"pattern": "X", "target_account": "Expenses:X", "match_field": "payee"}]'
        )
        migrate_legacy(tmp_path)
        rules = json.loads((tmp_path / ".beancount-importer" / "rules.json").read_text())
        assert len(rules) == 1
        # No tag state — top-level isn't a dict.
        assert not (tmp_path / ".beancount-importer" / "tag_state.json").exists()

    def test_rules_field_not_a_list(self, tmp_path: Path):
        # Legacy schema with `rules` typed as something other than a list →
        # treated as no rules at all.
        (tmp_path / ".import_config.json").write_text('{"rules": "not a list"}')
        migrate_legacy(tmp_path)
        rules = json.loads((tmp_path / ".beancount-importer" / "rules.json").read_text())
        assert rules == []

    def test_non_dict_rule_items_skipped(self, tmp_path: Path):
        # Mixed valid/garbage entries in the rules list — keep the dicts,
        # silently drop the rest.
        (tmp_path / ".import_config.json").write_text(json.dumps({
            "rules": [
                "scalar string",
                {"pattern": "X", "target_account": "Expenses:X", "match_field": "payee"},
                123,
            ],
        }))
        migrate_legacy(tmp_path)
        rules = json.loads((tmp_path / ".beancount-importer" / "rules.json").read_text())
        assert len(rules) == 1

    def test_rule_without_pattern_dropped(self, tmp_path: Path):
        # A rule with a target but no pattern would match every transaction;
        # silently drop it.
        _seed_legacy(
            tmp_path,
            rules=[{"target_account": "Expenses:Catchall", "match_field": "payee"}],
        )
        migrate_legacy(tmp_path)
        rules = json.loads((tmp_path / ".beancount-importer" / "rules.json").read_text())
        assert rules == []

    def test_rule_validation_error_skipped(self, tmp_path: Path):
        # A rule that fails Pydantic validation (invalid amount_sign coerce
        # path? Use a target_account that's empty after fall-through — actually
        # we need to provoke a real ValueError. An invalid `match_field=any`
        # WITHOUT a pattern returns []; a totally bogus payee_pattern can
        # fail at validate. Use a non-string target_account to provoke.
        _seed_legacy(
            tmp_path,
            rules=[
                # Bogus types → ValueError from Pydantic
                {"pattern": "OK", "target_account": 12345, "match_field": "payee"},
                # A valid one to verify migration continues
                {"pattern": "X", "target_account": "Expenses:X", "match_field": "payee"},
            ],
        )
        migrate_legacy(tmp_path)
        rules = json.loads((tmp_path / ".beancount-importer" / "rules.json").read_text())
        # The valid rule remains; the bogus one was silently skipped.
        assert any(r["target_account"] == "Expenses:X" for r in rules)

    def test_active_tag_invalid_mode_falls_back_to_always(self, tmp_path: Path):
        # An unrecognised `mode` should snap to "always" rather than raise —
        # the legacy script tolerated stale on-disk modes after a refactor.
        legacy = {
            "rules": [],
            "config": {
                "active_tag": {"tag": "trip", "mode": "weird-mode"},
                "recent_tags": [],
            },
        }
        (tmp_path / ".import_config.json").write_text(json.dumps(legacy))
        migrate_legacy(tmp_path)
        ts = json.loads((tmp_path / ".beancount-importer" / "tag_state.json").read_text())
        assert ts["active"]["mode"] == "always"

    def test_active_tag_value_not_dict_skipped(self, tmp_path: Path):
        # Garbage active_tag (non-dict) → no active payload.
        legacy = {
            "rules": [],
            "config": {
                "active_tag": "not a dict",
                "recent_tags": ["foo"],
            },
        }
        (tmp_path / ".import_config.json").write_text(json.dumps(legacy))
        migrate_legacy(tmp_path)
        ts = json.loads((tmp_path / ".beancount-importer" / "tag_state.json").read_text())
        # Recent tags persist, but no active.
        assert ts["active"] is None
        assert ts["recent"] == ["foo"]

    def test_recent_tags_not_list_yields_empty(self, tmp_path: Path):
        legacy = {
            "rules": [],
            "config": {
                "active_tag": {"tag": "trip", "mode": "always"},
                "recent_tags": "not a list",
            },
        }
        (tmp_path / ".import_config.json").write_text(json.dumps(legacy))
        migrate_legacy(tmp_path)
        ts = json.loads((tmp_path / ".beancount-importer" / "tag_state.json").read_text())
        assert ts["recent"] == []

    def test_skip_update_rules_corrupt_yields_empty(self, tmp_path: Path):
        # Top-level isn't a dict → skip patterns extraction returns [].
        (tmp_path / ".import_config.json").write_text("[]")
        migrate_legacy(tmp_path)
        cfg = tomllib.loads((tmp_path / ".beancount-importer" / "config.toml").read_text())
        assert cfg.get("skip_update_patterns", []) == []

    def test_skip_update_rules_non_list_yields_empty(self, tmp_path: Path):
        legacy = {
            "rules": [],
            "config": {"skip_update_rules": "not a list"},
        }
        (tmp_path / ".import_config.json").write_text(json.dumps(legacy))
        migrate_legacy(tmp_path)
        cfg = tomllib.loads((tmp_path / ".beancount-importer" / "config.toml").read_text())
        assert cfg.get("skip_update_patterns", []) == []

    def test_skip_update_rule_non_dict_skipped(self, tmp_path: Path):
        legacy = {
            "rules": [],
            "config": {
                "skip_update_rules": [
                    "garbage",
                    {"pattern": "Real", "match_field": "payee"},
                    42,
                ],
            },
        }
        (tmp_path / ".import_config.json").write_text(json.dumps(legacy))
        migrate_legacy(tmp_path)
        cfg = tomllib.loads((tmp_path / ".beancount-importer" / "config.toml").read_text())
        skip = cfg.get("skip_update_patterns", [])
        assert len(skip) == 1
        assert skip[0]["field"] == "payee"

    def test_skip_update_rule_without_pattern_dropped(self, tmp_path: Path):
        legacy = {
            "rules": [],
            "config": {
                "skip_update_rules": [
                    {"match_field": "payee"},  # no pattern → dropped
                    {"pattern": "Real", "match_field": "payee"},
                ],
            },
        }
        (tmp_path / ".import_config.json").write_text(json.dumps(legacy))
        migrate_legacy(tmp_path)
        cfg = tomllib.loads((tmp_path / ".beancount-importer" / "config.toml").read_text())
        skip = cfg.get("skip_update_patterns", [])
        assert len(skip) == 1

    def test_skip_update_rule_match_field_any_expands(self, tmp_path: Path):
        legacy = {
            "rules": [],
            "config": {
                "skip_update_rules": [
                    {"pattern": "Generic", "match_field": "any"},
                ],
            },
        }
        (tmp_path / ".import_config.json").write_text(json.dumps(legacy))
        migrate_legacy(tmp_path)
        cfg = tomllib.loads((tmp_path / ".beancount-importer" / "config.toml").read_text())
        skip = cfg.get("skip_update_patterns", [])
        # `any` expands to two entries (payee + narration).
        assert {p["field"] for p in skip} == {"payee", "narration"}

    def test_skip_update_rule_unknown_match_field_falls_back_to_narration(self, tmp_path: Path):
        legacy = {
            "rules": [],
            "config": {
                "skip_update_rules": [
                    {"pattern": "Mystery", "match_field": "unknown_thing"},
                ],
            },
        }
        (tmp_path / ".import_config.json").write_text(json.dumps(legacy))
        migrate_legacy(tmp_path)
        cfg = tomllib.loads((tmp_path / ".beancount-importer" / "config.toml").read_text())
        skip = cfg.get("skip_update_patterns", [])
        assert len(skip) == 1
        assert skip[0]["field"] == "narration"

    def test_suppress_lists_corrupt_legacy_yields_no_flags(self, tmp_path: Path):
        # `_legacy_suppress_lists` returns {} when the legacy file is absent
        # OR when its top-level isn't a dict — covers both cases.
        from beancount_importer.scaffolding import _legacy_suppress_lists
        assert _legacy_suppress_lists(tmp_path / "missing.json") == {}
        # Non-dict top-level
        p = tmp_path / "list.json"
        p.write_text("[]")
        assert _legacy_suppress_lists(p) == {}
        # Corrupt JSON
        bad = tmp_path / "bad.json"
        bad.write_text("not json")
        assert _legacy_suppress_lists(bad) == {}

    def test_extract_legacy_skip_patterns_corrupt(self, tmp_path: Path):
        # Direct unit cover for the matching defensive branches in
        # `_extract_legacy_skip_patterns`.
        from beancount_importer.scaffolding import _extract_legacy_skip_patterns
        # File missing
        assert _extract_legacy_skip_patterns(tmp_path / "missing.json") == []
        # Corrupt JSON
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert _extract_legacy_skip_patterns(bad) == []
        # Non-dict
        listy = tmp_path / "list.json"
        listy.write_text("[]")
        assert _extract_legacy_skip_patterns(listy) == []

    def test_convert_legacy_tag_state_no_data_returns_none(self, tmp_path: Path):
        # No active and no recent → function returns None entirely.
        from beancount_importer.scaffolding import _convert_legacy_tag_state
        # Missing file
        assert _convert_legacy_tag_state(tmp_path / "missing.json") is None
        # Corrupt JSON
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert _convert_legacy_tag_state(bad) is None
        # Non-dict top-level
        listy = tmp_path / "list.json"
        listy.write_text("[]")
        assert _convert_legacy_tag_state(listy) is None
        # Legitimate dict but no active/recent fields
        empty = tmp_path / "empty.json"
        empty.write_text(json.dumps({"rules": []}))
        assert _convert_legacy_tag_state(empty) is None

    def test_build_config_skips_unknown_bank_keys(self, tmp_path: Path):
        # Direct cover for `_build_config_toml`'s `if csv_defaults is None:
        # continue` branch — passing in a key that has no preset CSV defaults.
        from beancount_importer.scaffolding import _build_config_toml
        result = _build_config_toml(["spk", "unknown-bank"])
        keys = {b["key"] for b in result["banks"]}
        assert keys == {"spk"}

    def test_convert_legacy_rules_returns_empty_when_path_missing(self, tmp_path: Path):
        from beancount_importer.scaffolding import _convert_legacy_rules
        assert _convert_legacy_rules(tmp_path / "missing.json") == []

    def test_convert_legacy_rules_corrupt_json(self, tmp_path: Path):
        from beancount_importer.scaffolding import _convert_legacy_rules
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert _convert_legacy_rules(bad) == []

    def test_legacy_rule_to_new_returns_empty_without_pattern(self, tmp_path: Path):
        # A legacy rule with target_account but no pattern AND match_field
        # not "any" — function returns []. The `if not pattern` branch.
        from beancount_importer.scaffolding import _legacy_rule_to_new
        out = _legacy_rule_to_new({
            "target_account": "Expenses:X",
            "match_field": "payee",
        })
        assert out == []

    def test_has_meaningful_rules_corrupt_treated_as_meaningful(self, tmp_path: Path):
        # Don't clobber unparseable user content — the helper treats it
        # as meaningful so the migration leaves the file alone.
        from beancount_importer.scaffolding import _has_meaningful_rules
        p = tmp_path / "rules.json"
        p.write_text("{not json")
        assert _has_meaningful_rules(p) is True
