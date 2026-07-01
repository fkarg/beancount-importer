"""End-to-end CLI tests with a scripted categorizer.

`tests/test_cli.py` covers `--preview` and the KeyboardInterrupt
contract. These tests pick up where it stops: the *interactive* run
path. We monkeypatch the screen-categorizer / screen-merge-fn factories
so `cli.main()` constructs a `ScriptedCategorizer` instead of the Rich
prompt host, then drive a full Typer invocation via `CliRunner`.

What this covers that the unit tests don't:
- The factory wiring (`make_screen_categorizer(console)` actually getting
  called and its return value reaching `run_pipeline`).
- `_persist_results` writing the rendered `.bean` text the user can
  inspect on disk after a real run.
- `_persist_new_rules` round-tripping a save-as-rule pick to `rules.json`.
- Merge-prompt routing — that the CLI hands `MergeContext`s to the merge
  fn and writes (or skips) based on the returned `MergeDecision`.
"""

from __future__ import annotations

import subprocess
import textwrap
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beancount_importer.cli import app
from beancount_importer.models import CategoryProposal, Posting
from beancount_importer.pipeline import MergeDecision
from scripted import (
    ScriptedCategorizer,
    ScriptedMergeFn,
    categorize_as,
    skip_proposal,
)


CONFIG_TOML = textwrap.dedent("""\
    rules_file = "rules.json"
    decisions_file = "decisions.jsonl"
    tag_state_file = "tag_state.json"

    [matching]
    min_score = 0.35

    [[banks]]
    key = "spk"
    display_name = "Sparkasse"
    account = "Assets:B:SPK"
    file_glob = "documents/SPK_*.csv"
    output_file = "transactions/{year}/SPK.bean"

    [banks.csv]
    delimiter = ";"
    date_format = ["%d.%m.%y"]
    amount_locale = "de"
    field_date = "Buchungstag"
    field_amount = "Betrag"
    field_currency = "Waehrung"
    field_payee = "Beguenstigter"
    field_description = "Verwendungszweck"
""")


CSV_TWO_ROWS = textwrap.dedent("""\
    Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung
    15.01.24;Netflix;Netflix Abo;-15,99;EUR
    17.01.24;Rewe;REWE Filiale;-42,50;EUR
""")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "import_config.toml").write_text(CONFIG_TOML)
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "SPK_jan.csv").write_text(CSV_TWO_ROWS)
    (tmp_path / "transactions").mkdir()
    return tmp_path


def _inject(
    monkeypatch: pytest.MonkeyPatch,
    categorizer: ScriptedCategorizer,
    merge_fn: ScriptedMergeFn | None = None,
) -> None:
    """Swap the screen factories for ones returning our scripted doubles."""
    monkeypatch.setattr(
        "beancount_importer.cli.make_screen_categorizer",
        lambda _console: categorizer,
    )
    monkeypatch.setattr(
        "beancount_importer.cli.make_screen_merge_fn",
        lambda _console: merge_fn if merge_fn is not None else None,
    )


def _init_git_repo(d: Path) -> None:
    subprocess.run(["git", "init", "-q", str(d)], check=True)

    def run(*a: str) -> None:
        subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True)

    run("config", "user.email", "t@example.com")
    run("config", "user.name", "Test")
    run("add", "-A")
    run("commit", "-qm", "init")


def _head(d: Path) -> str:
    return subprocess.check_output(["git", "-C", str(d), "rev-parse", "HEAD"]).decode()


def _two_row_categorizer() -> ScriptedCategorizer:
    return ScriptedCategorizer({
        ("Netflix", Decimal("15.99")): categorize_as(
            "Expenses:Streaming", payee="Netflix", narration="Netflix Abo"),
        ("Rewe", Decimal("42.50")): categorize_as(
            "Expenses:Groceries", payee="Rewe", narration="REWE Filiale"),
    })


class TestAutoCommit:
    def test_commit_flag_commits_importer_files(self, project, monkeypatch):
        _init_git_repo(project)
        _inject(monkeypatch, _two_row_categorizer())
        before = _head(project)
        result = CliRunner().invoke(app, [
            "2024", "--config", str(project / "import_config.toml"), "--commit",
        ])
        assert result.exit_code == 0, result.output
        assert _head(project) != before  # a commit happened
        files = subprocess.check_output(
            ["git", "-C", str(project), "show", "--name-only", "--format=", "HEAD"]
        ).decode()
        assert "transactions/2024/SPK.bean" in files
        assert "decisions.jsonl" in files

    def test_config_default_commits_without_flag(self, project, monkeypatch):
        cfg = (project / "import_config.toml").read_text().replace(
            "[matching]", "auto_commit_after_run = true\n\n[matching]"
        )
        (project / "import_config.toml").write_text(cfg)
        _init_git_repo(project)
        _inject(monkeypatch, _two_row_categorizer())
        before = _head(project)
        result = CliRunner().invoke(
            app, ["2024", "--config", str(project / "import_config.toml")]
        )
        assert result.exit_code == 0, result.output
        assert _head(project) != before

    def test_no_commit_flag_overrides_config_default(self, project, monkeypatch):
        cfg = (project / "import_config.toml").read_text().replace(
            "[matching]", "auto_commit_after_run = true\n\n[matching]"
        )
        (project / "import_config.toml").write_text(cfg)
        _init_git_repo(project)
        _inject(monkeypatch, _two_row_categorizer())
        before = _head(project)
        result = CliRunner().invoke(app, [
            "2024", "--config", str(project / "import_config.toml"), "--no-commit",
        ])
        assert result.exit_code == 0, result.output
        assert _head(project) == before  # nothing committed

    def test_commit_in_non_git_dir_is_nonfatal(self, project, monkeypatch):
        # No git repo → the run still succeeds; the commit is skipped with a note.
        _inject(monkeypatch, _two_row_categorizer())
        result = CliRunner().invoke(app, [
            "2024", "--config", str(project / "import_config.toml"), "--commit",
        ])
        assert result.exit_code == 0, result.output
        assert "auto-commit skipped" in result.output


class TestCrashPreservesDecisions:
    def test_unexpected_crash_midrun_flushes_decisions_for_replay(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A crash mid-run is NOT a Ctrl+C abandon: the decisions made before the
        # crash must survive so a re-run replays them (the .bean is derived and
        # regenerated). Only KeyboardInterrupt drops them.
        calls: list[str] = []

        def crashing(ctx):
            calls.append(ctx.txn.payee or "")
            if len(calls) == 1:
                return CategoryProposal(
                    action="categorize",
                    postings=(Posting(account="Expenses:Streaming"),),
                    payee=ctx.txn.payee,
                    narration=ctx.txn.description,
                )
            raise RuntimeError("boom mid-run")

        monkeypatch.setattr(
            "beancount_importer.cli.make_screen_categorizer", lambda _c: crashing
        )
        monkeypatch.setattr(
            "beancount_importer.cli.make_screen_merge_fn", lambda _c: None
        )

        result = CliRunner().invoke(
            app, ["2024", "--config", str(project / "import_config.toml")]
        )
        # The crash still surfaces (non-zero exit), but the first decision was
        # persisted before it propagated.
        assert result.exit_code != 0
        decisions_file = project / "decisions.jsonl"
        assert decisions_file.exists()
        lines = [ln for ln in decisions_file.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1
        assert "Netflix" in lines[0]


class TestInteractiveRunWritesLedger:
    def test_two_rows_categorized_produce_two_entries(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        scripted = ScriptedCategorizer(
            {
                ("Netflix", Decimal("15.99")): categorize_as(
                    "Expenses:Streaming",
                    payee="Netflix",
                    narration="Netflix Abo",
                ),
                ("Rewe", Decimal("42.50")): categorize_as(
                    "Expenses:Groceries",
                    payee="Rewe",
                    narration="REWE Filiale",
                ),
            }
        )
        _inject(monkeypatch, scripted)

        result = CliRunner().invoke(
            app,
            [
                "2024",
                "--config",
                str(project / "import_config.toml"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert len(scripted.calls) == 2

        out = (project / "transactions" / "2024" / "SPK.bean").read_text()
        assert 'Netflix' in out
        assert 'Expenses:Streaming' in out
        assert 'Rewe' in out
        assert 'Expenses:Groceries' in out

    def test_save_as_rule_writes_to_rules_json(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # User hits [r] on the Netflix row → pipeline synthesizes a rule
        # via `_derive_rule` and `_persist_new_rules` should append it
        # to rules.json on a successful run.
        scripted = ScriptedCategorizer(
            {
                ("Netflix", Decimal("15.99")): categorize_as(
                    "Expenses:Streaming",
                    payee="Netflix",
                    narration="Netflix Abo",
                    save_as_rule=True,
                ),
                ("Rewe", Decimal("42.50")): categorize_as(
                    "Expenses:Groceries",
                    payee="Rewe",
                    narration="REWE Filiale",
                ),
            }
        )
        _inject(monkeypatch, scripted)

        result = CliRunner().invoke(
            app,
            ["2024", "--config", str(project / "import_config.toml")],
        )
        assert result.exit_code == 0, result.output

        rules_text = (project / "rules.json").read_text()
        assert "Netflix" in rules_text
        assert "Expenses:Streaming" in rules_text
        # Rewe wasn't saved-as-rule, must not leak in.
        assert "Rewe" not in rules_text

    def test_skip_action_writes_no_entry(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A `skip` proposal from the categorizer means the row is
        # dropped from this run; no .bean entry, no rule, no diff to
        # apply. The other row still flows through normally.
        scripted = ScriptedCategorizer(
            {
                ("Netflix", Decimal("15.99")): skip_proposal(),
                ("Rewe", Decimal("42.50")): categorize_as(
                    "Expenses:Groceries",
                    payee="Rewe",
                    narration="REWE Filiale",
                ),
            }
        )
        _inject(monkeypatch, scripted)

        result = CliRunner().invoke(
            app,
            ["2024", "--config", str(project / "import_config.toml")],
        )
        assert result.exit_code == 0, result.output

        out = (project / "transactions" / "2024" / "SPK.bean").read_text()
        assert "Rewe" in out
        assert "Netflix" not in out


class TestMergePromptRouting:
    """The merge prompt fires when a real diff exists between the user's
    proposal and the matched entry. Three engineering choices in setup:

    1. Date offset (6 days) so dedup misses (≤5) but scorer catches (≤7).
       Without this, dedup silent-skips before we ever score.

    2. Seeded entry's target_account differs from what we'll propose.
       The auto-seed for a rule-less candidate would just reuse the
       candidate's target — same target, no diff, pipeline silent-skips.

    3. Pre-installed rule that proposes the new target. The rule's seed
       diffs against the entry's old target, defeating silent-skip and
       routing through the categorizer → merge prompt path.
    """

    def _seed_ledger_and_rule(self, project: Path) -> None:
        year_dir = project / "transactions" / "2024"
        year_dir.mkdir(parents=True, exist_ok=True)
        # CSV row 2024-01-15, ledger entry 2024-01-09 — 6 days apart.
        # Entry's target (`Subscriptions`) differs from the rule's
        # target (`Streaming`) installed below.
        (year_dir / "SPK.bean").write_text(textwrap.dedent("""\
            2024-01-09 * "Netflix" "old narration"
              Assets:B:SPK                             -15.99 EUR
              Expenses:Subscriptions
        """))
        # Pre-install a rule so the rule's seed proposal has a target
        # that diffs against the entry's, defeating silent-skip.
        (project / "rules.json").write_text(textwrap.dedent("""\
            [
              {
                "payee_pattern": "Netflix",
                "target_account": "Expenses:Streaming",
                "bank_key": "spk"
              }
            ]
        """))

    def test_keep_branch_writes_nothing(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        self._seed_ledger_and_rule(project)
        scripted = ScriptedCategorizer(
            {
                ("Netflix", Decimal("15.99")): categorize_as(
                    "Expenses:Streaming",
                    payee="Netflix",
                    narration="Netflix Abo",  # would-be overwrite
                ),
                ("Rewe", Decimal("42.50")): categorize_as(
                    "Expenses:Groceries",
                    payee="Rewe",
                    narration="REWE Filiale",
                ),
            }
        )
        # `keep` → silent-match; the existing entry stays as-is.
        merge = ScriptedMergeFn(
            {("Netflix", Decimal("15.99")): MergeDecision(action="keep")}
        )
        _inject(monkeypatch, scripted, merge)

        result = CliRunner().invoke(
            app,
            ["2024", "--config", str(project / "import_config.toml")],
        )
        assert result.exit_code == 0, result.output
        assert len(merge.calls) == 1  # only the Netflix row hit the merge prompt

        out = (project / "transactions" / "2024" / "SPK.bean").read_text()
        # Original narration preserved — `keep` declined the rewrite.
        assert '"old narration"' in out
        assert "Netflix Abo" not in out

    def test_skip_branch_leaves_entry_untouched(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        self._seed_ledger_and_rule(project)
        scripted = ScriptedCategorizer(
            {
                ("Netflix", Decimal("15.99")): categorize_as(
                    "Expenses:Streaming",
                    payee="Netflix",
                    narration="Netflix Abo",
                ),
                ("Rewe", Decimal("42.50")): categorize_as(
                    "Expenses:Groceries",
                    payee="Rewe",
                    narration="REWE Filiale",
                ),
            }
        )
        merge = ScriptedMergeFn(
            {("Netflix", Decimal("15.99")): MergeDecision(action="skip")}
        )
        _inject(monkeypatch, scripted, merge)

        result = CliRunner().invoke(
            app,
            ["2024", "--config", str(project / "import_config.toml")],
        )
        assert result.exit_code == 0, result.output

        out = (project / "transactions" / "2024" / "SPK.bean").read_text()
        # `skip` → no rewrite, but also no new entry; Netflix stays
        # exactly as seeded. Rewe still gets appended.
        assert '"old narration"' in out
        assert "Rewe" in out
