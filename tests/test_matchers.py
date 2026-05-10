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
) -> LedgerEntry:
    """An entry that the user booked with `paypal:` (or similar) metadata.

    `entry_date` is the merchant/actual date; `metadata_dates` carries the
    settle/intermediary dates extracted from the entry's posting metadata.
    """
    return LedgerEntry(
        date=entry_date,
        narration="Uber",
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
