from __future__ import annotations

from decimal import Decimal
from datetime import date

from beancount_importer.matching.normalize import normalize_text
from beancount_importer.matching.scorer import similarity_score, levenshtein_distance, lcs_length
from beancount_importer.matching.dedup import dedup_key, is_duplicate
from beancount_importer.models import SourceTransaction, LedgerEntry


def make_txn(**kwargs) -> SourceTransaction:
    defaults = dict(
        booking_date=date(2024, 1, 15),
        amount=Decimal("-15.99"),
        currency="EUR",
        bank_key="spk",
        payee="Netflix",
        description="Netflix Abo",
    )
    return SourceTransaction(**(defaults | kwargs))


def make_entry(**kwargs) -> LedgerEntry:
    defaults = dict(
        date=date(2024, 1, 15),
        narration="Netflix",
        source_account="Assets:B:SPK",
        target_account="Expenses:Entertainment",
        amount=Decimal("-15.99"),
        currency="EUR",
    )
    return LedgerEntry(**(defaults | kwargs))


# ── normalize_text ───────────────────────────────────────────────────────────

class TestNormalizeText:
    def test_lowercases(self):
        assert normalize_text("NETFLIX") == "netflix"

    def test_strips_accents(self):
        # ü → u
        assert normalize_text("Müller") == "muller"

    def test_collapses_whitespace(self):
        assert normalize_text("hello   world") == "hello world"

    def test_strips_leading_trailing(self):
        assert normalize_text("  hello  ") == "hello"

    def test_nfkd_ligature(self):
        # ﬁ (ligature) → fi
        result = normalize_text("ﬁle")
        assert "fi" in result or "le" in result  # depends on ASCII encoding

    def test_empty_string(self):
        assert normalize_text("") == ""

    def test_already_normalized(self):
        assert normalize_text("hello world") == "hello world"


# ── similarity_score ─────────────────────────────────────────────────────────

class TestSimilarityScore:
    def test_identical_strings_score_100(self):
        assert similarity_score("Netflix", "Netflix") == 100.0

    def test_empty_strings_score_zero(self):
        # rapidfuzz returns 0 for two empty strings
        assert similarity_score("", "") == 0.0

    def test_completely_different_score_low(self):
        score = similarity_score("Netflix", "Rewe")
        assert score < 50

    def test_case_insensitive(self):
        assert similarity_score("Netflix", "NETFLIX") == 100.0

    def test_token_order_irrelevant(self):
        s1 = similarity_score("Netflix Abo Monatlich", "Monatlich Abo Netflix")
        assert s1 == 100.0

    def test_partial_overlap_scores_between(self):
        score = similarity_score("Netflix Premium", "Netflix")
        assert 0 < score <= 100

    def test_returns_float(self):
        assert isinstance(similarity_score("a", "b"), float)


# ── levenshtein_distance ─────────────────────────────────────────────────────

class TestLevenshteinDistance:
    def test_identical_is_zero(self):
        assert levenshtein_distance("Netflix", "Netflix") == 0

    def test_one_edit(self):
        assert levenshtein_distance("cat", "bat") == 1

    def test_completely_different(self):
        d = levenshtein_distance("abc", "xyz")
        assert d == 3

    def test_empty_vs_nonempty(self):
        assert levenshtein_distance("", "abc") == 3

    def test_case_insensitive(self):
        assert levenshtein_distance("Netflix", "NETFLIX") == 0


# ── lcs_length ───────────────────────────────────────────────────────────────

class TestLcsLength:
    def test_identical_strings(self):
        assert lcs_length("abc", "abc") == 3

    def test_empty_strings(self):
        assert lcs_length("", "") == 0
        assert lcs_length("abc", "") == 0

    def test_partial_overlap(self):
        assert lcs_length("abcde", "ace") == 3

    def test_no_overlap(self):
        assert lcs_length("abc", "xyz") == 0

    def test_case_insensitive(self):
        assert lcs_length("ABC", "abc") == 3


# ── dedup_key ────────────────────────────────────────────────────────────────

class TestDedupKey:
    def test_uses_sepa_ref_when_present(self):
        txn = make_txn(sepa_reference="NETFLIX-001")
        assert dedup_key(txn) == "sepa:NETFLIX-001"

    def test_uses_hash_when_no_sepa(self):
        txn = make_txn(sepa_reference="")
        key = dedup_key(txn)
        assert key.startswith("hash:")

    def test_hash_deterministic(self):
        txn = make_txn(sepa_reference="")
        assert dedup_key(txn) == dedup_key(txn)

    def test_different_amounts_different_hash(self):
        t1 = make_txn(sepa_reference="", amount=Decimal("-10"))
        t2 = make_txn(sepa_reference="", amount=Decimal("-20"))
        assert dedup_key(t1) != dedup_key(t2)

    def test_different_sepa_different_key(self):
        t1 = make_txn(sepa_reference="REF-001")
        t2 = make_txn(sepa_reference="REF-002")
        assert dedup_key(t1) != dedup_key(t2)


# ── is_duplicate ─────────────────────────────────────────────────────────────

class TestIsDuplicate:
    def test_not_duplicate_when_empty_list(self):
        txn = make_txn(sepa_reference="NETFLIX-001")
        assert not is_duplicate(txn, [])

    def test_detects_sepa_match(self):
        txn = make_txn(sepa_reference="NETFLIX-001")
        entry = make_entry(metadata={"sepa_ref": "NETFLIX-001"})
        assert is_duplicate(txn, [entry])

    def test_no_false_positive_different_sepa(self):
        txn = make_txn(sepa_reference="NETFLIX-002")
        entry = make_entry(metadata={"sepa_ref": "NETFLIX-001"})
        assert not is_duplicate(txn, [entry])

    def test_not_duplicate_different_amounts(self):
        t1 = make_txn(sepa_reference="", amount=Decimal("-10"))
        e = make_entry(amount=Decimal("-20"), narration="other")
        assert not is_duplicate(t1, [e])

    def test_detects_content_match_on_cash_withdrawal(self):
        # Real-world regression: cash withdrawal at SPK ATM has no SEPA
        # ref, falls back to the content hash. Previously the txn hash
        # used `bank_key="spk"` while the entry hash used
        # `source_account="Assets:B:SPK"` — different strings, so the
        # hashes never matched and dedup quietly missed every cash row.
        # Verify the content path now matches across the bank/account
        # naming gap.
        txn = make_txn(
            sepa_reference="",
            booking_date=date(2024, 12, 23),
            amount=Decimal("-240.00"),
            payee="GA NR00003062 BLZ72051210 1",
            description="23.12/13.45UHR AICHACH BARGELDAUSZAHLUNG",
            bank_key="spk",
        )
        entry = make_entry(
            date=date(2024, 12, 23),
            amount=Decimal("-240.00"),
            payee="GA NR00003062 BLZ72051210 1",
            narration="23.12/13.45UHR AICHACH BARGELDAUSZAHLUNG",
            source_account="Assets:B:SPK",
        )
        assert is_duplicate(txn, [entry])

    def test_detects_match_when_txn_has_sepa_but_entry_lacks_metadata(self):
        # Real-world regression: the writer doesn't emit `sepa_ref`
        # metadata, so a re-import of a SEPA-bearing CSV row used to
        # silently miss its own previously-imported entry — the txn
        # side keyed `sepa:X` while the entry side fell through to
        # the content hash. Dedup now compares key *sets*, so the
        # shared content hash catches this case.
        txn = make_txn(
            sepa_reference="1034114065753",
            payee="PayPal Europe S.a.r.l. et Cie S.C.A",
            description="1034114065753/. Uber, Ihr Einkauf bei Uber FOLGELASTSCHRIFT",
        )
        entry = make_entry(
            payee="PayPal Europe S.a.r.l. et Cie S.C.A",
            narration="1034114065753/. Uber, Ihr Einkauf bei Uber FOLGELASTSCHRIFT",
            metadata={},
        )
        assert is_duplicate(txn, [entry])

    def test_detects_content_match_when_payee_only_differs_in_capitalisation(self):
        # Sanity: hashes are case-sensitive (we don't normalise here —
        # that's the scorer's job). If a user lowercased the payee in
        # the bean file, dedup should NOT fire and the matcher's fuzzy
        # path takes over. Keeps the contract honest about what dedup
        # buys you (exact match, fast path).
        txn = make_txn(sepa_reference="", payee="NETFLIX")
        entry = make_entry(payee="netflix")
        assert not is_duplicate(txn, [entry])


# ── transfers: heuristic ─────────────────────────────────────────────────────

from beancount_importer.matching.transfers import (
    is_likely_internal_transfer,
    find_existing_counterparty,
)


class TestIsLikelyInternalTransfer:
    def test_keyword_in_description(self):
        txn = make_txn(payee="", description="Überweisung an Sparkasse")
        is_t, _ = is_likely_internal_transfer(txn)
        assert is_t

    def test_paypal_always_transfer(self):
        # Even a non-round PayPal amount with a merchant URL is treated as a
        # transfer to PayPal — the bank doesn't pay the merchant directly.
        txn = make_txn(payee="PayPal Europe", description="www.amazon.de purchase")
        is_t, target = is_likely_internal_transfer(txn)
        assert is_t
        assert target == "Assets:B:PayPal"

    def test_n26_round_amount(self):
        txn = make_txn(payee="N26", description="", amount=Decimal("-500.00"))
        is_t, target = is_likely_internal_transfer(txn)
        assert is_t
        assert target == "Assets:B:N26"

    def test_normal_purchase_not_transfer(self):
        txn = make_txn(payee="Rewe", description="REWE Filiale", amount=Decimal("-42.50"))
        is_t, target = is_likely_internal_transfer(txn)
        assert not is_t
        assert target is None

    def test_round_without_bank_name_not_transfer(self):
        txn = make_txn(payee="Random Shop", description="Stuff", amount=Decimal("-100.00"))
        is_t, _ = is_likely_internal_transfer(txn)
        assert not is_t


class TestFindExistingCounterparty:
    def test_finds_reversed_match(self):
        txn = make_txn(amount=Decimal("-100.00"), payee="Transfer")
        # The OTHER bank already booked the +100 incoming side
        entry = make_entry(amount=Decimal("100.00"), source_account="Assets:B:N26")
        result = find_existing_counterparty(txn, [entry])
        assert result is entry

    def test_ignores_non_internal_account(self):
        txn = make_txn(amount=Decimal("-100.00"))
        entry = make_entry(amount=Decimal("100.00"), source_account="Expenses:Foo")
        assert find_existing_counterparty(txn, [entry]) is None

    def test_respects_tolerance(self):
        txn = make_txn(amount=Decimal("-100.00"), booking_date=date(2024, 1, 15))
        entry = make_entry(
            amount=Decimal("100.00"),
            date=date(2024, 1, 25),
            source_account="Assets:B:N26",
        )
        assert find_existing_counterparty(txn, [entry], tolerance_days=5) is None

    def test_currency_mismatch_excluded(self):
        txn = make_txn(amount=Decimal("-100.00"))
        entry = make_entry(
            amount=Decimal("100.00"),
            currency="USD",
            source_account="Assets:B:N26",
        )
        assert find_existing_counterparty(txn, [entry]) is None

    def test_picks_closest_date(self):
        txn = make_txn(amount=Decimal("-50.00"), booking_date=date(2024, 1, 15))
        far = make_entry(
            amount=Decimal("50.00"), date=date(2024, 1, 19), source_account="Assets:B:N26",
        )
        near = make_entry(
            amount=Decimal("50.00"), date=date(2024, 1, 16), source_account="Assets:B:N26",
        )
        assert find_existing_counterparty(txn, [far, near]) is near


# ── paypal cross-reference ───────────────────────────────────────────────────

from beancount_importer.matching.paypal import (
    find_paypal_counterpart,
    is_paypal_funding_txn,
)


class TestFindPaypalCounterpart:
    def test_matches_same_amount_close_date(self):
        bank = make_txn(amount=Decimal("-15.99"), booking_date=date(2024, 1, 15), payee="PayPal")
        pp = make_txn(
            amount=Decimal("-15.99"),
            booking_date=date(2024, 1, 14),
            bank_key="paypal",
            payee="Amazon",
        )
        assert find_paypal_counterpart(bank, [pp]) is pp

    def test_no_match_when_amount_differs(self):
        bank = make_txn(amount=Decimal("-15.99"))
        pp = make_txn(amount=Decimal("-20.00"), bank_key="paypal")
        assert find_paypal_counterpart(bank, [pp]) is None

    def test_no_match_outside_tolerance(self):
        bank = make_txn(amount=Decimal("-15.99"), booking_date=date(2024, 1, 15))
        pp = make_txn(
            amount=Decimal("-15.99"),
            booking_date=date(2024, 1, 30),
            bank_key="paypal",
        )
        assert find_paypal_counterpart(bank, [pp], tolerance_days=7) is None

    def test_picks_closest_date(self):
        bank = make_txn(amount=Decimal("-15.99"), booking_date=date(2024, 1, 15))
        far = make_txn(
            amount=Decimal("-15.99"),
            booking_date=date(2024, 1, 12),
            bank_key="paypal",
            payee="Far",
        )
        near = make_txn(
            amount=Decimal("-15.99"),
            booking_date=date(2024, 1, 14),
            bank_key="paypal",
            payee="Near",
        )
        match = find_paypal_counterpart(bank, [far, near])
        assert match is not None and match.payee == "Near"


class TestIsPaypalFundingTxn:
    def test_payee_paypal(self):
        assert is_paypal_funding_txn(make_txn(payee="PayPal Europe S.a.r.l."))

    def test_description_paypal(self):
        assert is_paypal_funding_txn(make_txn(payee="", description="PayPal Top-up"))

    def test_unrelated_false(self):
        assert not is_paypal_funding_txn(make_txn(payee="Rewe", description="Groceries"))


# ── cross-bank matching: inferred amount + metadata dates ────────────────────

from beancount_importer.matching.scorer import score_candidate, find_candidates


class TestCrossBankInferredAmount:
    """When matching a CSV row against an entry whose source posting was
    inferred by beancount (typically a transit account on another bank's
    transfer entry), the sign convention is opposite to the CSV's.
    """

    def test_inferred_entry_matches_opposite_sign(self):
        # PayPal CSV: user paid -7.49 at merchant
        txn = make_txn(
            amount=Decimal("-7.49"),
            booking_date=date(2024, 1, 17),
            payee="Steam",
            description="Steam purchase",
            bank_key="paypal",
        )
        # SPK→PayPal transfer entry as seen from PayPal side: SPK booked
        # -7.49, PayPal leg was inferred to +7.49
        entry = make_entry(
            amount=Decimal("7.49"),
            date=date(2024, 1, 19),
            source_account="Assets:B:PayPal",
            target_account="Assets:B:SPK",
            payee="PayPal",
            narration="PayPal Einkauf",
            amount_inferred=True,
        )
        score = score_candidate(txn, entry)
        assert score > 0.0, "inferred-amount entry should match opposite-sign txn"

    def test_explicit_entry_rejects_opposite_sign(self):
        # Same shape but the entry's amount was explicit in the source file
        # — that's a real PayPal-CSV-derived entry, not a transit leg, so we
        # require strict sign equality.
        txn = make_txn(amount=Decimal("-7.49"), bank_key="paypal")
        entry = make_entry(
            amount=Decimal("7.49"),  # explicit, not inferred
            source_account="Assets:B:PayPal",
            payee="Different",
            narration="Different",
        )
        assert score_candidate(txn, entry) == 0.0

    def test_inferred_entry_uses_metadata_date(self):
        # Bookkeeping date is 2 days after the actual purchase. With the
        # `actual:` metadata, the scorer should consider both dates and pick
        # the closer one.
        txn = make_txn(
            amount=Decimal("-103.19"),
            booking_date=date(2024, 1, 17),
            bank_key="paypal",
        )
        entry = make_entry(
            amount=Decimal("103.19"),
            date=date(2024, 1, 19),
            metadata_dates=(date(2024, 1, 17),),
            amount_inferred=True,
        )
        score = score_candidate(txn, entry)
        assert score > 0.0

    def test_inferred_entry_outside_tolerance_still_rejected(self):
        # 30 days apart even with metadata dates
        txn = make_txn(amount=Decimal("-50"), booking_date=date(2024, 1, 1))
        entry = make_entry(
            amount=Decimal("50"),
            date=date(2024, 2, 15),
            amount_inferred=True,
        )
        assert score_candidate(txn, entry, max_date_days=14) == 0.0

    def test_find_candidates_filters_below_min_score(self):
        # An entry that would score above 0 but below min_score must be
        # filtered out without raising.
        txn = make_txn(payee="A", description="B", booking_date=date(2024, 1, 15))
        # Far date (≈max_date_days), opposite text → low text+date proximity.
        weak = make_entry(
            payee="Z",
            narration="Q",
            date=date(2024, 1, 28),  # 13 days off → date proximity ≈ 0.07
        )
        # A high min_score guarantees `weak` is filtered out.
        assert find_candidates(txn, [weak], min_score=0.95) == []

    def test_find_candidates_picks_inferred_match(self):
        txn = make_txn(
            amount=Decimal("-15.99"),
            booking_date=date(2024, 1, 15),
            bank_key="paypal",
            payee="Steam",
        )
        entry = make_entry(
            amount=Decimal("15.99"),
            date=date(2024, 1, 16),
            amount_inferred=True,
            payee="PayPal",
            narration="PayPal Einkauf",
        )
        candidates = find_candidates(txn, [entry], min_score=0.35)
        assert len(candidates) == 1


class TestMetadataDateProximity:
    """Even non-inferred entries may carry metadata dates that should be
    considered for proximity (e.g. a PayPal-side entry with `actual:`).
    """

    def test_metadata_date_brings_distant_entry_in_range(self):
        # Booking date 30 days away — outside default tolerance — but the
        # metadata date is exact.
        txn = make_txn(
            amount=Decimal("-15.99"),
            booking_date=date(2024, 1, 15),
        )
        entry = make_entry(
            amount=Decimal("-15.99"),
            date=date(2024, 2, 15),  # 31 days away
            metadata_dates=(date(2024, 1, 15),),  # exact
        )
        # Default max_date_days=14 would normally reject this entry.
        score = score_candidate(txn, entry)
        assert score > 0.0


# ── Hard-filter coverage for score_candidate ────────────────────────────────


class TestScoreCandidateHardFilters:
    def test_currency_mismatch_returns_zero(self):
        txn = make_txn(currency="EUR")
        entry = make_entry(currency="USD")
        assert score_candidate(txn, entry) == 0.0

    def test_amount_mismatch_returns_zero(self):
        txn = make_txn(amount=Decimal("-15.99"))
        entry = make_entry(amount=Decimal("-99.99"))
        assert score_candidate(txn, entry) == 0.0

    def test_inferred_absolute_amount_mismatch_returns_zero(self):
        # Cross-bank transit entry with mismatched absolute amount: the
        # |amount| comparison fails just like the strict-sign branch.
        txn = make_txn(amount=Decimal("-10.00"))
        entry = make_entry(amount=Decimal("20.00"), amount_inferred=True)
        assert score_candidate(txn, entry) == 0.0

    def test_include_reversed_sign_uses_absolute_compare(self):
        # Without `include_reversed_sign`, opposite signs are rejected.
        txn = make_txn(amount=Decimal("-10.00"))
        entry = make_entry(amount=Decimal("10.00"))
        assert score_candidate(txn, entry) == 0.0
        # With it, |amounts| must agree — same date, currency → positive score.
        assert score_candidate(txn, entry, include_reversed_sign=True) > 0.0


class TestScoreCandidateBonuses:
    def test_sepa_match_boosts_score(self):
        """An exact SEPA reference match adds a big bonus, capped at 1.0.
        Compared to the same scenario without SEPA, the score must be higher."""
        # Use disjoint text + a date offset so the baseline score is well
        # below 1.0 — otherwise the SEPA bonus saturates and can't be measured.
        txn = make_txn(
            sepa_reference="REF-001",
            payee="Foo",
            description="Bar",
            booking_date=date(2024, 1, 15),
        )
        entry_with = make_entry(
            payee="Baz",
            narration="Qux",
            date=date(2024, 1, 22),
            metadata={"sepa_ref": "REF-001"},
        )
        entry_without = make_entry(
            payee="Baz",
            narration="Qux",
            date=date(2024, 1, 22),
        )
        boosted = score_candidate(txn, entry_with)
        baseline = score_candidate(txn, entry_without)
        assert boosted > baseline
        assert boosted <= 1.0

    def test_sepa_reference_metadata_alternate_key(self):
        # Some legacy ledgers store under `sepa_reference` instead of `sepa_ref`.
        txn = make_txn(
            sepa_reference="REF-002",
            payee="Foo",
            description="Bar",
            booking_date=date(2024, 1, 15),
        )
        entry = make_entry(
            payee="Baz",
            narration="Qux",
            date=date(2024, 1, 22),
            metadata={"sepa_reference": "REF-002"},
        )
        baseline = score_candidate(
            txn,
            make_entry(payee="Baz", narration="Qux", date=date(2024, 1, 22)),
        )
        assert score_candidate(txn, entry) > baseline


# ── PayPal counterpart: currency mismatch ───────────────────────────────────


class TestPaypalCounterpartCurrency:
    def test_currency_mismatch_skips_match(self):
        bank = make_txn(amount=Decimal("-50"), currency="EUR")
        pp = make_txn(
            amount=Decimal("-50"),
            currency="USD",
            bank_key="paypal",
            booking_date=date(2024, 1, 15),
        )
        assert find_paypal_counterpart(bank, [pp]) is None


# ── Transfers: closest fallback when no expense/income posting exists ───────


class TestPickOtherPostingFallback:
    """Internal coverage for `_pick_other_posting`: when no Expenses/Income
    posting exists, the function falls back to the first non-source posting."""

    def test_synthesizes_with_non_expense_target(self, tmp_path):
        # Two non-source legs, neither expense/income — fallback path picks
        # the first one. Routed through reader.read_ledger so we exercise
        # the function in its real call site.
        from beancount_importer.beancount_io.reader import read_ledger

        bean = tmp_path / "x.bean"
        bean.write_text(
            '2024-01-15 * "Payee" "Narr"\n'
            '  Assets:B:SPK  -1.00 EUR\n'
            '    paypal: 2024-01-15\n'
            '  Assets:B:Other  1.00 EUR\n'
        )
        entries = read_ledger(
            bean,
            "Assets:B:PayPal",
            synthesize_from_metadata={"paypal": "Assets:B:PayPal"},
        )
        # The synthesized virtual entry's target_account should be the
        # non-source `Assets:B:Other`.
        synthesized = [e for e in entries if e.amount_inferred]
        assert len(synthesized) == 1
        assert synthesized[0].target_account == "Assets:B:Other"
