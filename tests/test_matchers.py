"""Tests for the cross-source matcher registry and the two shipped hooks."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from beancount_importer.matching.paypal import hook as paypal_hook
from beancount_importer.matching.registry import (
    MatcherHook,
    MatchOutcome,
    first_outcome,
    load_matchers,
)
from beancount_importer.matching.settled import hook as settled_hook
from beancount_importer.matching.transfers import hook as transfers_hook
from beancount_importer.models import LedgerEntry, SourceTransaction


def _spk_funding(amount: Decimal = Decimal("-3.39")) -> SourceTransaction:
    return SourceTransaction(
        booking_date=date(2024, 4, 13),
        amount=amount,
        currency="EUR",
        payee="PayPal",
        description="PayPal Einkauf 12345",
        bank_key="spk",
    )


def _paypal_csv(amount: Decimal = Decimal("-3.39")) -> SourceTransaction:
    return SourceTransaction(
        booking_date=date(2024, 4, 13),
        amount=amount,
        currency="EUR",
        payee="Google Payment",
        description="Google",
        bank_key="paypal",
    )


def _n26_transfer() -> SourceTransaction:
    return SourceTransaction(
        booking_date=date(2024, 5, 1),
        amount=Decimal("-50.00"),
        currency="EUR",
        payee="Sparkasse",
        description="Überweisung an SPK",
        bank_key="n26",
    )


def _existing_spk_leg() -> LedgerEntry:
    return LedgerEntry(
        date=date(2024, 5, 1),
        narration="Transfer in",
        source_account="Assets:B:SPK",
        target_account="Assets:B:N26",
        amount=Decimal("50.00"),
        currency="EUR",
    )


# ── Registry ──────────────────────────────────────────────────────────────────


class TestRegistry:
    def test_load_matchers_imports_dotted_paths(self):
        hooks = load_matchers(
            [
                "beancount_importer.matching.transfers",
                "beancount_importer.matching.paypal",
            ]
        )
        assert len(hooks) == 2
        assert all(isinstance(h, MatcherHook) for h in hooks)

    def test_load_matchers_rejects_missing_hook(self, tmp_path: Path, monkeypatch):
        monkeypatch.syspath_prepend(str(tmp_path))
        (tmp_path / "no_hook.py").write_text("# nothing\n")
        with pytest.raises(TypeError, match="no top-level `hook`"):
            load_matchers(["no_hook"])

    def test_load_matchers_rejects_wrong_shape(self, tmp_path: Path, monkeypatch):
        monkeypatch.syspath_prepend(str(tmp_path))
        # `hook` exists but lacks the required `match` method.
        (tmp_path / "bad_hook.py").write_text(
            "class _H:\n    name = 'x'\nhook = _H()\n"
        )
        with pytest.raises(TypeError, match="does not satisfy"):
            load_matchers(["bad_hook"])

    def test_first_outcome_short_circuits(self):
        called: list[str] = []

        class _Yes:
            name = "yes"

            def match(self, txn, all_csv_by_bank, existing_entries):
                del txn, all_csv_by_bank, existing_entries
                called.append("yes")
                return MatchOutcome(kind="skip", reason="r")

        class _No:
            name = "no"

            def match(self, txn, all_csv_by_bank, existing_entries):
                del txn, all_csv_by_bank, existing_entries
                called.append("no")
                return None

        # Yes-first → No is never called.
        outcome = first_outcome([_Yes(), _No()], _spk_funding(), {}, [])
        assert outcome is not None and outcome.kind == "skip"
        assert called == ["yes"]

        called.clear()
        # No-first → Yes still wins, both called.
        outcome = first_outcome([_No(), _Yes()], _spk_funding(), {}, [])
        assert outcome is not None and outcome.kind == "skip"
        assert called == ["no", "yes"]

    def test_first_outcome_returns_none_when_no_hook_fires(self):
        class _Pass:
            name = "pass"

            def match(self, *a, **kw):
                del a, kw
                return None

        assert first_outcome([_Pass()], _spk_funding(), {}, []) is None


# ── PayPal counterpart matcher ────────────────────────────────────────────────


class TestPayPalMatcher:
    def test_rewrites_funding_row_to_paypal_account(self):
        spk = _spk_funding(Decimal("-3.39"))
        paypal_csv = _paypal_csv(Decimal("-3.39"))
        outcome = paypal_hook.match(
            spk,
            {"paypal": [paypal_csv]},
            [],
        )
        assert outcome is not None
        assert outcome.kind == "rewrite_target"
        assert outcome.target_account == "Assets:B:PayPal"
        assert outcome.metadata == {"paypal": "2024-04-13"}
        assert outcome.matched_txn is paypal_csv

    def test_does_not_match_when_paypal_csv_has_no_counterpart(self):
        spk = _spk_funding(Decimal("-3.39"))
        outcome = paypal_hook.match(spk, {"paypal": []}, [])
        assert outcome is None

    def test_skips_when_row_does_not_look_like_paypal_funding(self):
        # No "PayPal" in payee/description → cheap pre-filter rejects.
        non_paypal = SourceTransaction(
            booking_date=date(2024, 4, 13),
            amount=Decimal("-10.00"),
            currency="EUR",
            payee="Rewe",
            description="REWE Filiale",
            bank_key="spk",
        )
        outcome = paypal_hook.match(
            non_paypal,
            {"paypal": [_paypal_csv()]},
            [],
        )
        assert outcome is None

    def test_paypal_side_row_not_self_matched(self):
        # The PayPal CSV row itself is on bank_key="paypal"; the matcher must
        # not try to find a counterpart for it, otherwise it'd loop.
        pp = _paypal_csv()
        outcome = paypal_hook.match(pp, {"paypal": [pp]}, [])
        assert outcome is None


# ── Existing-transfer matcher ─────────────────────────────────────────────────


class TestTransferMatcher:
    def test_skips_row_when_counterparty_already_booked(self):
        n26 = _n26_transfer()
        outcome = transfers_hook.match(n26, {}, [_existing_spk_leg()])
        assert outcome is not None
        assert outcome.kind == "skip"
        assert outcome.reason == "counterpart_already_booked"
        assert outcome.matched_entry is not None
        assert outcome.matched_entry.source_account == "Assets:B:SPK"

    def test_does_not_skip_non_transfer_rows(self):
        # No transfer keyword → matcher passes.
        merchant = SourceTransaction(
            booking_date=date(2024, 5, 1),
            amount=Decimal("-12.34"),
            currency="EUR",
            payee="Rewe",
            description="REWE Filiale",
            bank_key="n26",
        )
        outcome = transfers_hook.match(
            merchant, {}, [_existing_spk_leg()]
        )
        assert outcome is None

    def test_does_not_skip_when_no_counterparty_exists(self):
        outcome = transfers_hook.match(_n26_transfer(), {}, [])
        assert outcome is None


# ── Intermediary-settlement matcher ───────────────────────────────────────────


def _settled_entry(
    *,
    entry_date: date = date(2024, 4, 29),
    metadata_dates: tuple[date, ...] = (date(2024, 5, 3),),
    amount: Decimal = Decimal("-29.06"),
    currency: str = "EUR",
    payee: str = "PayPal",
    narration: str = "Uber",
) -> LedgerEntry:
    """An entry that the user booked with `paypal:` (or similar) metadata.

    `entry_date` is the merchant/actual date; `metadata_dates` carries the
    settle/intermediary dates extracted from the entry's posting metadata.
    The default `payee="PayPal"` matches realistic bookings — the bank-side
    entry typically tags both the intermediary and the merchant so the
    matcher's text floor lines up with PayPal-CSV rows on either the
    merchant ("Uber") or sign-flipped funding ("PayPal Bank Deposit") side.
    """
    return LedgerEntry(
        date=entry_date,
        payee=payee,
        narration=narration,
        source_account="Assets:B:SPK",
        target_account="Expenses:Food:Outside",
        amount=amount,
        currency=currency,
        metadata_dates=metadata_dates,
    )


class TestIntermediarySettlementMatcher:
    def test_skips_when_csv_row_matches_settle_date(self):
        # PayPal CSV row dated at the settle (intermediary) date — entry's
        # `paypal: 2024-05-03` metadata says we already booked this.
        txn = SourceTransaction(
            booking_date=date(2024, 5, 3),
            amount=Decimal("-29.06"),
            currency="EUR",
            payee="Uber",
            description="Uber",
            bank_key="paypal",
        )
        outcome = settled_hook.match(txn, {}, [_settled_entry()])
        assert outcome is not None
        assert outcome.kind == "skip"
        assert outcome.reason == "settled_via_intermediary"
        assert outcome.matched_entry is not None

    def test_skips_when_csv_row_matches_actual_date(self):
        # CSV row dated at the entry's own (actual) date — also a duplicate.
        txn = SourceTransaction(
            booking_date=date(2024, 4, 29),
            amount=Decimal("-29.06"),
            currency="EUR",
            payee="Uber",
            description="Uber",
            bank_key="paypal",
        )
        outcome = settled_hook.match(txn, {}, [_settled_entry()])
        assert outcome is not None
        assert outcome.kind == "skip"

    def test_skips_when_sign_flips_across_banks(self):
        # SPK records the outflow (-29.06); PayPal "Bank Deposit" inflow
        # (+29.06) on the same date — same logical movement, opposite sign.
        txn = SourceTransaction(
            booking_date=date(2024, 5, 3),
            amount=Decimal("29.06"),
            currency="EUR",
            payee="PayPal Bank Deposit",
            description="Bank Deposit",
            bank_key="paypal",
        )
        outcome = settled_hook.match(txn, {}, [_settled_entry()])
        assert outcome is not None
        assert outcome.kind == "skip"

    def test_does_not_skip_entry_without_metadata_dates(self):
        # A regular merchant entry (no paypal:/settle:) is NOT settlement
        # evidence; the matcher must pass so dedup/scoring can run normally.
        plain = LedgerEntry(
            date=date(2024, 4, 29),
            narration="Uber",
            source_account="Assets:B:SPK",
            target_account="Expenses:Food:Outside",
            amount=Decimal("-29.06"),
            currency="EUR",
        )
        txn = SourceTransaction(
            booking_date=date(2024, 4, 29),
            amount=Decimal("-29.06"),
            currency="EUR",
            payee="Uber",
            description="Uber",
            bank_key="paypal",
        )
        assert settled_hook.match(txn, {}, [plain]) is None

    def test_does_not_skip_on_amount_mismatch(self):
        txn = SourceTransaction(
            booking_date=date(2024, 5, 3),
            amount=Decimal("-30.00"),
            currency="EUR",
            payee="Uber",
            description="Uber",
            bank_key="paypal",
        )
        assert settled_hook.match(txn, {}, [_settled_entry()]) is None

    def test_does_not_skip_on_currency_mismatch(self):
        txn = SourceTransaction(
            booking_date=date(2024, 5, 3),
            amount=Decimal("-29.06"),
            currency="USD",
            payee="Uber",
            description="Uber",
            bank_key="paypal",
        )
        assert settled_hook.match(txn, {}, [_settled_entry()]) is None

    def test_does_not_skip_when_dates_disagree(self):
        # Same amount, but neither the entry's date nor its metadata_dates
        # match — the row is a separate transaction.
        txn = SourceTransaction(
            booking_date=date(2024, 6, 15),
            amount=Decimal("-29.06"),
            currency="EUR",
            payee="Uber",
            description="Uber",
            bank_key="paypal",
        )
        assert settled_hook.match(txn, {}, [_settled_entry()]) is None

    def test_does_not_skip_unrelated_payee_with_matching_amount_and_date(self):
        # Recurring fixed-amount risk: an unrelated 29.06 EUR row landing
        # on the same date as some other entry's `paypal:` metadata. The
        # text floor must reject — silent-skipping a real transaction is
        # how rows disappear without warning.
        unrelated = SourceTransaction(
            booking_date=date(2024, 5, 3),
            amount=Decimal("-29.06"),
            currency="EUR",
            payee="Spotify",
            description="Premium Family",
            bank_key="paypal",
        )
        assert settled_hook.match(unrelated, {}, [_settled_entry()]) is None

    def test_does_not_skip_when_csv_row_has_no_text(self):
        # Without identifying text on the CSV side the matcher can't tell
        # a real settlement from an amount/date coincidence — fall through
        # to dedup/scoring instead of silent-skipping.
        textless = SourceTransaction(
            booking_date=date(2024, 5, 3),
            amount=Decimal("-29.06"),
            currency="EUR",
            bank_key="paypal",
        )
        assert settled_hook.match(textless, {}, [_settled_entry()]) is None

    def test_does_not_skip_when_entry_has_no_text(self):
        # Mirror image: a nameless ledger entry isn't strong enough
        # evidence to silent-skip a CSV row even if amount + date align.
        textless_entry = LedgerEntry(
            date=date(2024, 4, 29),
            narration="",
            source_account="Assets:B:SPK",
            target_account="Expenses:Food:Outside",
            amount=Decimal("-29.06"),
            currency="EUR",
            metadata_dates=(date(2024, 5, 3),),
        )
        txn = SourceTransaction(
            booking_date=date(2024, 5, 3),
            amount=Decimal("-29.06"),
            currency="EUR",
            payee="Uber",
            description="Uber",
            bank_key="paypal",
        )
        assert settled_hook.match(txn, {}, [textless_entry]) is None

    def test_picks_first_text_matching_entry_when_amount_collides(self):
        # Two settlement-bearing entries with the same |amount| + date.
        # The matcher must prefer the one whose text actually overlaps
        # with the CSV row, not the first iterated.
        wrong = _settled_entry(payee="PayPal", narration="Spotify")
        right = _settled_entry(payee="PayPal", narration="Uber")
        txn = SourceTransaction(
            booking_date=date(2024, 5, 3),
            amount=Decimal("-29.06"),
            currency="EUR",
            payee="Uber",
            description="Uber Trip",
            bank_key="paypal",
        )
        # Iterate `wrong` first — text floor rejects it; matcher then
        # falls through to `right`.
        outcome = settled_hook.match(txn, {}, [wrong, right])
        assert outcome is not None
        assert outcome.matched_entry is right


# ── Default registration order ────────────────────────────────────────────────


class TestDefaultMatcherOrder:
    """The shipped `enabled_matchers` order is load-bearing: each later
    matcher assumes the earlier ones have already filtered. Specifically,
    `intermediary_settlement` must run before `paypal` — otherwise a
    row that *should* silent-skip would instead get rewritten to the
    PayPal account and re-booked.
    """

    def test_settled_runs_before_paypal_in_default_config(self):
        from beancount_importer.config import MatchingConfig

        cfg = MatchingConfig()
        order = cfg.enabled_matchers
        assert "beancount_importer.matching.settled" in order
        assert "beancount_importer.matching.paypal" in order
        assert order.index(
            "beancount_importer.matching.settled"
        ) < order.index("beancount_importer.matching.paypal")

    def test_settled_pre_empts_paypal_when_both_would_match(self):
        # SPK funding row with a settled-on-PayPal counterpart entry AND
        # a PayPal-CSV row: paypal_hook would emit `rewrite_target`,
        # settled_hook would emit `skip`. The default load order must
        # produce `skip`, otherwise an already-booked row gets re-imported.
        spk = _spk_funding(Decimal("-3.39"))
        paypal_csv_row = _paypal_csv(Decimal("-3.39"))
        already_booked = LedgerEntry(
            date=date(2024, 4, 13),
            payee="PayPal",
            narration="Google",
            source_account="Assets:B:SPK",
            target_account="Expenses:Subscriptions",
            amount=Decimal("-3.39"),
            currency="EUR",
            metadata_dates=(date(2024, 4, 13),),
        )
        hooks = load_matchers(
            [
                "beancount_importer.matching.settled",
                "beancount_importer.matching.paypal",
            ]
        )
        outcome = first_outcome(
            hooks, spk, {"paypal": [paypal_csv_row]}, [already_booked]
        )
        assert outcome is not None
        assert outcome.kind == "skip"
        assert outcome.reason == "settled_via_intermediary"


# ── via_paypal placeholder linking ───────────────────────────────────────────

from beancount_importer.matching.via_paypal import hook as via_paypal_hook


def _placeholder_entry(**overrides) -> LedgerEntry:
    defaults = dict(
        date=date(2024, 4, 15),
        payee="Penny",
        narration="PayPal",
        source_account="Assets:B:SPK",
        target_account="Expenses:Food:Groceries",
        amount=Decimal("-7.81"),
        currency="EUR",
        metadata={"via_paypal": "True", "sepa_ref": "REF-P"},
        file_path="SPK.bean",
        line_start=1,
    )
    defaults.update(overrides)
    return LedgerEntry(**defaults)


def _paypal_purchase(**overrides) -> SourceTransaction:
    defaults = dict(
        booking_date=date(2024, 4, 13),
        amount=Decimal("-7.81"),
        currency="EUR",
        payee="Penny Markt",
        description="Penny Markt",
        bank_key="paypal",
    )
    defaults.update(overrides)
    return SourceTransaction(**defaults)


class TestViaPaypalMatcher:
    """Pairs a PayPal CSV row with a `via_paypal: TRUE` placeholder entry:
    exact signed amount, same currency, booking dates within 7 days, text
    floor, unique candidate. Emits `link_placeholder` carrying the entry.
    """

    def test_links_placeholder(self):
        entry = _placeholder_entry()
        outcome = via_paypal_hook.match(_paypal_purchase(), {}, [entry])
        assert outcome is not None
        assert outcome.kind == "link_placeholder"
        assert outcome.matched_entry is entry

    def test_marker_value_case_insensitive(self):
        # Old-importer files carry bare `TRUE` (bool → str "True" via the
        # reader); hand-written ones may carry the string "TRUE".
        entry = _placeholder_entry(metadata={"via_paypal": "TRUE"})
        outcome = via_paypal_hook.match(_paypal_purchase(), {}, [entry])
        assert outcome is not None

    def test_no_marker_no_match(self):
        entry = _placeholder_entry(metadata={"sepa_ref": "REF-P"})
        assert via_paypal_hook.match(_paypal_purchase(), {}, [entry]) is None

    def test_sign_must_match_exactly(self):
        # A +7.81 deposit row must not link a -7.81 purchase placeholder.
        txn = _paypal_purchase(amount=Decimal("7.81"))
        assert via_paypal_hook.match(txn, {}, [_placeholder_entry()]) is None

    def test_date_window_seven_days(self):
        txn = _paypal_purchase(booking_date=date(2024, 4, 4))
        assert via_paypal_hook.match(txn, {}, [_placeholder_entry()]) is None
        txn = _paypal_purchase(booking_date=date(2024, 4, 8))
        assert via_paypal_hook.match(txn, {}, [_placeholder_entry()]) is not None

    def test_text_floor_blocks_coincidence(self):
        txn = _paypal_purchase(payee="Zzqx", description="Zzqx")
        assert via_paypal_hook.match(txn, {}, [_placeholder_entry()]) is None

    def test_empty_txn_text_abstains(self):
        txn = _paypal_purchase(payee=None, description=None)
        assert via_paypal_hook.match(txn, {}, [_placeholder_entry()]) is None

    def test_ambiguous_candidates_abstain(self):
        e1 = _placeholder_entry()
        e2 = _placeholder_entry(date=date(2024, 4, 16))
        assert via_paypal_hook.match(_paypal_purchase(), {}, [e1, e2]) is None

    def test_multi_posting_placeholder_abstains(self):
        entry = _placeholder_entry(has_multiple_postings=True)
        assert via_paypal_hook.match(_paypal_purchase(), {}, [entry]) is None

    def test_currency_must_match(self):
        entry = _placeholder_entry(currency="USD")
        assert via_paypal_hook.match(_paypal_purchase(), {}, [entry]) is None
