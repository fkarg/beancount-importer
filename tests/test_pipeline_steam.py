"""End-to-end: Steam purchases get their game titles filled in at import."""

from __future__ import annotations

from pathlib import Path

from beancount_importer.config import BankConfig, Config, CsvConfig, MatchingConfig
from beancount_importer.models import CategoryProposal, Posting
from beancount_importer.pipeline import CategorizeContext, NoopReporter, run
from beancount_importer.rules.models import CategorizationRule
from beancount_importer.rules.tags import TagState
from beancount_importer.session import ImportOptions, ImportSession


def _spk_bank() -> BankConfig:
    return BankConfig(
        key="spk",
        display_name="Sparkasse",
        account="Assets:B:SPK",
        file_glob="SPK_*.csv",
        output_file="spk.bean",
        csv=CsvConfig(
            delimiter=";",
            date_format=["%d.%m.%y"],
            amount_locale="de",
            field_date="Buchungstag",
            field_amount="Betrag",
            field_currency="Waehrung",
            field_payee="Beguenstigter",
            field_description="Verwendungszweck",
            field_sepa_reference="Kundenreferenz",
        ),
    )


def _categorize_unknown(ctx: CategorizeContext) -> CategoryProposal:
    return CategoryProposal(
        action="categorize", postings=(Posting(account="Expenses:Unknown"),)
    )


def _steam_session(tmp_path: Path, *, steam_file: str | None) -> ImportSession:
    (tmp_path / "SPK_feb.csv").write_text(
        "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung;Kundenreferenz\n"
        "14.02.25;www.steampowered.com;Steam;-27,51;EUR;STEAM-1\n"
    )
    (tmp_path / "steam_history.csv").write_text(
        '"Date","Items","Type","Total","Wallet Change","Wallet Balance"\n'
        '"14 Feb, 2025","Noita\nBomber Crew","Purchase","27,51€","",""\n',
        encoding="utf-8-sig",
    )
    cfg = Config(
        banks=[_spk_bank()],
        matching=MatchingConfig(min_score=0.35),
        steam_history_file=steam_file,
    )
    rule = CategorizationRule(target_account="Expenses:Games", payee_pattern="steampowered")
    return ImportSession(config=cfg, rules=(rule,), tag_state=TagState(), options=ImportOptions())


def test_steam_purchase_enriched_with_titles(tmp_path: Path):
    session = _steam_session(tmp_path, steam_file="steam_history.csv")
    results = run(session, tmp_path, _categorize_unknown, NoopReporter())
    text = results[0].new_entry_text
    assert "Steam: Noita, Bomber Crew" in text
    assert 'games: "Noita, Bomber Crew"' in text


def test_no_steam_config_leaves_narration_alone(tmp_path: Path):
    session = _steam_session(tmp_path, steam_file=None)
    results = run(session, tmp_path, _categorize_unknown, NoopReporter())
    text = results[0].new_entry_text
    assert "Steam: Noita" not in text
    assert "games:" not in text
