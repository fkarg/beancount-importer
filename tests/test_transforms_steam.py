from datetime import date
from decimal import Decimal

from beancount_importer.models import CategoryProposal, Posting, SourceTransaction
from beancount_importer.rules.models import CategorizationRule
from beancount_importer.transforms.steam import (
    SteamEnricher,
    load_steam_index,
)

STEAM = "www.steampowered.com"


def make_txn(**kwargs) -> SourceTransaction:
    defaults = dict(
        booking_date=date(2025, 2, 14),
        amount=Decimal("-27.51"),
        currency="EUR",
        bank_key="paypal",
        payee=STEAM,
    )
    return SourceTransaction(**(defaults | kwargs))


def make_proposal(**kwargs) -> CategoryProposal:
    defaults = dict(
        action="categorize",
        postings=(Posting(account="Expenses:Games"),),
        narration="Games ???",
    )
    return CategoryProposal(**(defaults | kwargs))


def make_rule(**kwargs) -> CategorizationRule:
    return CategorizationRule(**(dict(target_account="Expenses:Games") | kwargs))


def write_csv(tmp_path, body: str):
    path = tmp_path / "steam_history.csv"
    path.write_text(
        '"Date","Items","Type","Total","Wallet Change","Wallet Balance"\n' + body,
        encoding="utf-8-sig",
    )
    return path


class TestLoadSteamIndex:
    def test_indexes_single_title_by_date_and_amount(self, tmp_path):
        path = write_csv(tmp_path, '"14 Feb, 2025","Noita","Purchase","18,80€","",""\n')
        index = load_steam_index(path)
        assert index[(date(2025, 2, 14), Decimal("18.80"))] == [["Noita"]]

    def test_splits_multiline_items_into_title_list(self, tmp_path):
        path = write_csv(
            tmp_path,
            '"9 Dec, 2025","Frostpunk\nNo Man\'s Sky\nCyberpunk 2077","Purchase","65,20€","",""\n',
        )
        index = load_steam_index(path)
        assert index[(date(2025, 12, 9), Decimal("65.20"))] == [
            ["Frostpunk", "No Man's Sky", "Cyberpunk 2077"]
        ]

    def test_parses_dashed_cents_as_whole_euros(self, tmp_path):
        path = write_csv(tmp_path, '"1 Jan, 2025","Foo","Purchase","32.--€","",""\n')
        index = load_steam_index(path)
        assert index[(date(2025, 1, 1), Decimal("32"))] == [["Foo"]]

    def test_unescapes_html_entities_in_titles(self, tmp_path):
        path = write_csv(
            tmp_path, '"1 Jan, 2025","Assassin&rsquo;s Creed","Purchase","10,00€","",""\n'
        )
        index = load_steam_index(path)
        assert index[(date(2025, 1, 1), Decimal("10.00"))] == [["Assassin’s Creed"]]

    def test_skips_rows_with_empty_total(self, tmp_path):
        path = write_csv(tmp_path, '"1 Jan, 2025","Wallet top-up","In-Game Purchase","","5,00€","5,00€"\n')
        assert load_steam_index(path) == {}

    def test_skips_rows_with_unparseable_date(self, tmp_path):
        path = write_csv(tmp_path, '"garbage","Foo","Purchase","10,00€","",""\n')
        assert load_steam_index(path) == {}

    def test_skips_rows_with_non_numeric_total(self, tmp_path):
        path = write_csv(tmp_path, '"1 Jan, 2025","Foo","Purchase","free€","",""\n')
        assert load_steam_index(path) == {}

    def test_skips_rows_with_no_titles(self, tmp_path):
        path = write_csv(tmp_path, '"1 Jan, 2025","","Purchase","10,00€","",""\n')
        assert load_steam_index(path) == {}

    def test_collision_keeps_both_candidates(self, tmp_path):
        path = write_csv(
            tmp_path,
            '"1 Jan, 2025","Foo","Purchase","10,00€","",""\n'
            '"1 Jan, 2025","Bar","Purchase","10,00€","",""\n',
        )
        index = load_steam_index(path)
        assert index[(date(2025, 1, 1), Decimal("10.00"))] == [["Foo"], ["Bar"]]


class TestSteamEnricher:
    def test_applies_to_is_always_true(self):
        assert SteamEnricher({}).applies_to(make_rule())

    def test_unique_two_titles_uses_full_list_in_narration(self):
        index = {(date(2025, 2, 14), Decimal("27.51")): [["Circuit Superstars", "Wreckfest"]]}
        out = SteamEnricher(index).apply(make_proposal(), make_txn(), make_rule())
        assert out.narration == "Steam: Circuit Superstars, Wreckfest"
        assert out.metadata["games"] == "Circuit Superstars, Wreckfest"

    def test_unique_many_titles_summarizes_narration(self):
        index = {(date(2025, 2, 14), Decimal("27.51")): [["A", "B", "C", "D"]]}
        out = SteamEnricher(index).apply(make_proposal(), make_txn(), make_rule())
        assert out.narration == "Steam: A, B (+2 more)"
        assert out.metadata["games"] == "A, B, C, D"

    def test_non_steam_payee_left_untouched(self):
        index = {(date(2025, 2, 14), Decimal("27.51")): [["Noita"]]}
        txn = make_txn(payee="REWE", description=None)
        out = SteamEnricher(index).apply(make_proposal(), txn, make_rule())
        assert out == make_proposal()

    def test_matches_on_description_when_payee_missing(self):
        index = {(date(2025, 2, 14), Decimal("27.51")): [["Noita"]]}
        txn = make_txn(payee=None, description="www.steampowered.com")
        out = SteamEnricher(index).apply(make_proposal(), txn, make_rule())
        assert out.metadata["games"] == "Noita"

    def test_no_index_hit_left_untouched(self):
        out = SteamEnricher({}).apply(make_proposal(), make_txn(), make_rule())
        assert out == make_proposal()

    def test_ambiguous_hit_left_untouched(self):
        index = {(date(2025, 2, 14), Decimal("27.51")): [["Foo"], ["Bar"]]}
        out = SteamEnricher(index).apply(make_proposal(), make_txn(), make_rule())
        assert out == make_proposal()

    def test_preserves_existing_metadata(self):
        index = {(date(2025, 2, 14), Decimal("27.51")): [["Noita"]]}
        proposal = make_proposal(metadata={"tag": "x"})
        out = SteamEnricher(index).apply(proposal, make_txn(), make_rule())
        assert out.metadata == {"tag": "x", "games": "Noita"}

    def test_whole_euro_amount_matches_two_decimal_txn(self):
        index = {(date(2025, 1, 1), Decimal("32")): [["Foo"]]}
        txn = make_txn(booking_date=date(2025, 1, 1), amount=Decimal("-32.00"))
        out = SteamEnricher(index).apply(make_proposal(), txn, make_rule())
        assert out.metadata["games"] == "Foo"

    def test_double_quotes_in_titles_sanitized(self):
        index = {(date(2025, 2, 14), Decimal("27.51")): [['A "quoted" game']]}
        out = SteamEnricher(index).apply(make_proposal(), make_txn(), make_rule())
        assert '"' not in out.narration
        assert '"' not in out.metadata["games"]
