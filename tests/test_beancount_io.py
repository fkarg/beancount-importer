from __future__ import annotations

from decimal import Decimal
from datetime import date
from pathlib import Path

import pytest

from beancount_importer.beancount_io.reader import read_ledger
from beancount_importer.beancount_io.writer import append_entry, format_transaction, splice_entries

FIXTURES = Path(__file__).parent / "fixtures"


# ── reader ───────────────────────────────────────────────────────────────────

class TestReadLedger:
    def test_reads_three_transactions(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert len(entries) == 3

    def test_missing_file_returns_empty(self, tmp_path: Path):
        entries = read_ledger(tmp_path / "nonexistent.bean", "Assets:B:SPK")
        assert entries == []

    def test_first_entry_date(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].date == date(2024, 1, 15)

    def test_first_entry_narration(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].narration == "Netflix Abo"

    def test_first_entry_payee(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].payee == "Netflix"

    def test_first_entry_amount(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].amount == Decimal("-15.99")

    def test_first_entry_currency(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].currency == "EUR"

    def test_source_account(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].source_account == "Assets:B:SPK"

    def test_target_account(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].target_account == "Expenses:Entertainment"

    def test_sepa_ref_in_metadata(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        assert entries[0].metadata.get("sepa_ref") == "NETFLIX-001"

    def test_positive_amount_salary(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:SPK")
        salary = next(e for e in entries if "Gehalt" in e.narration)
        assert salary.amount == Decimal("3000.00")

    def test_no_entries_for_wrong_account(self):
        entries = read_ledger(FIXTURES / "sample.bean", "Assets:B:N26")
        assert entries == []


class TestReadLedgerInferredAmount:
    """beancount fills in a missing posting amount during load. The reader
    needs to flag those entries (`amount_inferred=True`) so the scorer can
    treat them as cross-bank transit legs.
    """

    def _write(self, path: Path, content: str) -> None:
        path.write_text(content)

    def test_explicit_amount_not_marked_inferred(self, tmp_path: Path):
        bean = tmp_path / "spk.bean"
        self._write(
            bean,
            '2024-01-15 * "Netflix" "Abo"\n'
            '  Assets:B:SPK  -15.99 EUR\n'
            '  Expenses:Entertainment  15.99 EUR\n',
        )
        entries = read_ledger(bean, "Assets:B:SPK")
        assert entries[0].amount_inferred is False

    def test_inferred_amount_marked(self, tmp_path: Path):
        # SPK→PayPal transfer: SPK has explicit amount, PayPal leg is inferred.
        bean = tmp_path / "spk.bean"
        self._write(
            bean,
            '2024-04-18 * "PayPal" "Einkauf"\n'
            '  Assets:B:SPK  -10.00 EUR\n'
            '  Assets:B:PayPal\n',
        )
        entries = read_ledger(bean, "Assets:B:PayPal")
        assert len(entries) == 1
        assert entries[0].amount == Decimal("10.00")
        assert entries[0].amount_inferred is True


class TestReadLedgerMetadataDates:
    def _write(self, path: Path, content: str) -> None:
        path.write_text(content)

    def test_extracts_actual_date(self, tmp_path: Path):
        bean = tmp_path / "spk.bean"
        self._write(
            bean,
            '2024-01-19 * "Merchant" "PayPal"\n'
            '  Assets:B:SPK  -103.19 EUR\n'
            '  Assets:B:PayPal\n'
            '    actual: 2024-01-17\n',
        )
        entries = read_ledger(bean, "Assets:B:PayPal")
        assert len(entries) == 1
        assert date(2024, 1, 17) in entries[0].metadata_dates

    def test_unconfigured_keys_ignored(self, tmp_path: Path):
        bean = tmp_path / "spk.bean"
        self._write(
            bean,
            '2024-01-19 * "Merchant" "PayPal"\n'
            '  Assets:B:SPK  -100 EUR\n'
            '  Assets:B:PayPal\n'
            '    actual: 2024-01-17\n',
        )
        # Only consider `paypal:` — `actual:` should be ignored.
        entries = read_ledger(
            bean, "Assets:B:PayPal", metadata_date_keys=("paypal",)
        )
        assert entries[0].metadata_dates == ()


class TestReadLedgerSynthesize:
    """When a user runs plugins that split transactions at load time, the
    bean file may carry only a metadata hint (`paypal: 2024-01-17`). The
    reader can synthesise the virtual entry the plugin would produce so
    that cross-bank PayPal matching works without loading plugins.
    """

    def _write(self, path: Path, content: str) -> None:
        path.write_text(content)

    def test_synthesizes_paypal_entry_from_metadata(self, tmp_path: Path):
        bean = tmp_path / "spk.bean"
        # Bank-side entry where the user's plugin would split off a PayPal
        # transaction on the actual purchase date.
        self._write(
            bean,
            '2024-04-13 * "Google Payment" ""\n'
            '  Assets:B:SPK  -3.39 EUR\n'
            '    paypal: 2024-04-11\n'
            '  Expenses:Apps  3.39 EUR\n',
        )
        entries = read_ledger(
            bean,
            "Assets:B:PayPal",
            synthesize_from_metadata={"paypal": "Assets:B:PayPal"},
        )
        # No real PayPal posting → only the synthesized one.
        assert len(entries) == 1
        e = entries[0]
        assert e.date == date(2024, 4, 11)
        assert e.amount == Decimal("-3.39")
        assert e.target_account == "Expenses:Apps"
        assert e.amount_inferred is True

    def test_synthesize_ignored_when_account_doesnt_match(self, tmp_path: Path):
        bean = tmp_path / "spk.bean"
        self._write(
            bean,
            '2024-04-13 * "Google Payment" ""\n'
            '  Assets:B:SPK  -3.39 EUR\n'
            '    paypal: 2024-04-11\n'
            '  Expenses:Apps  3.39 EUR\n',
        )
        # Loading for SPK shouldn't trigger PayPal synthesis.
        entries = read_ledger(
            bean,
            "Assets:B:SPK",
            synthesize_from_metadata={"paypal": "Assets:B:PayPal"},
        )
        assert len(entries) == 1
        assert entries[0].source_account == "Assets:B:SPK"
        assert entries[0].amount_inferred is False

    def test_unparseable_metadata_date_skips_synthesis(self, tmp_path: Path):
        # A user-typed `paypal: "garbage"` shouldn't crash or synthesise — it
        # should be silently ignored. _coerce_date returns None and the
        # synthesise loop continues without emitting.
        bean = tmp_path / "spk.bean"
        self._write(
            bean,
            '2024-04-13 * "Google Payment" ""\n'
            '  Assets:B:SPK  -3.39 EUR\n'
            '    paypal: "garbage-date"\n'
            '  Expenses:Apps  3.39 EUR\n',
        )
        entries = read_ledger(
            bean,
            "Assets:B:PayPal",
            synthesize_from_metadata={"paypal": "Assets:B:PayPal"},
        )
        # No virtual entry was synthesised — the unparseable date was ignored.
        assert entries == []

    def test_quoted_string_metadata_date_parsed(self, tmp_path: Path):
        # When the user types `paypal: "2024-04-11"` (note the quotes) the
        # value comes through as `str` rather than `datetime.date`. The
        # reader's `_coerce_date` must handle that path too.
        bean = tmp_path / "spk.bean"
        self._write(
            bean,
            '2024-04-13 * "Google Payment" ""\n'
            '  Assets:B:SPK  -3.39 EUR\n'
            '    paypal: "2024-04-11"\n'
            '  Expenses:Apps  3.39 EUR\n',
        )
        entries = read_ledger(
            bean,
            "Assets:B:PayPal",
            synthesize_from_metadata={"paypal": "Assets:B:PayPal"},
        )
        assert len(entries) == 1
        assert entries[0].date == date(2024, 4, 11)

    def test_quoted_dotted_date_format_parsed(self, tmp_path: Path):
        # Some legacy ledgers wrote dates as `paypal: "11.04.2024"`. The
        # reader cycles through ISO, dotted, and slash formats — covers
        # the second item in the format tuple.
        bean = tmp_path / "spk.bean"
        self._write(
            bean,
            '2024-04-13 * "Google Payment" ""\n'
            '  Assets:B:SPK  -3.39 EUR\n'
            '    paypal: "11.04.2024"\n'
            '  Expenses:Apps  3.39 EUR\n',
        )
        entries = read_ledger(
            bean,
            "Assets:B:PayPal",
            synthesize_from_metadata={"paypal": "Assets:B:PayPal"},
        )
        assert len(entries) == 1
        assert entries[0].date == date(2024, 4, 11)

    def test_synthesize_picks_first_expense_when_multiple(self, tmp_path: Path):
        # Three postings: source bank + two Expenses legs. `_pick_other_posting`
        # must keep the FIRST Expenses match and skip subsequent ones.
        bean = tmp_path / "spk.bean"
        self._write(
            bean,
            '2024-04-13 * "Y" ""\n'
            '  Assets:B:SPK  -10.00 EUR\n'
            '    paypal: 2024-04-11\n'
            '  Expenses:First  6.00 EUR\n'
            '  Expenses:Second  4.00 EUR\n',
        )
        entries = read_ledger(
            bean,
            "Assets:B:PayPal",
            synthesize_from_metadata={"paypal": "Assets:B:PayPal"},
        )
        assert len(entries) == 1
        assert entries[0].target_account == "Expenses:First"

    def test_pick_other_posting_returns_none_when_only_source(self, tmp_path: Path):
        # Edge case: a transaction with only one posting (the source). Beancount
        # does accept such structures via padding/automatic — exercise the
        # `_pick_other_posting` returning None path.
        from beancount_importer.beancount_io.reader import _pick_other_posting
        from collections import namedtuple

        FakePosting = namedtuple("FakePosting", ["account"])
        src = FakePosting(account="Assets:B:SPK")
        # Single posting list containing only the source: no "other" exists.
        assert _pick_other_posting([src], src) is None

    def test_pick_other_posting_falls_back_to_non_expense(self):
        # Cover the `elif fallback is None` branch in _pick_other_posting:
        # when no Expenses/Income posting exists, the first non-source
        # posting is used.
        from beancount_importer.beancount_io.reader import _pick_other_posting
        from collections import namedtuple

        FakePosting = namedtuple("FakePosting", ["account"])
        src = FakePosting(account="Assets:B:SPK")
        other = FakePosting(account="Assets:B:N26")
        third = FakePosting(account="Assets:B:Other")
        # Multiple non-expense postings — fallback picks the first non-source
        # one and ignores the rest.
        assert _pick_other_posting([src, other, third], src) is other


class TestCoerceDate:
    """Unit tests for `_coerce_date` covering each input shape it accepts."""

    def test_date_passthrough(self):
        from beancount_importer.beancount_io.reader import _coerce_date
        d = date(2024, 1, 15)
        assert _coerce_date(d) == d

    def test_datetime_truncated_to_date(self):
        from datetime import datetime
        from beancount_importer.beancount_io.reader import _coerce_date
        # Beancount metadata can carry either `date` or `datetime`; the
        # latter must be reduced to a date so callers compare apples-to-apples.
        assert _coerce_date(datetime(2024, 1, 15, 10, 30)) == date(2024, 1, 15)

    def test_iso_string(self):
        from beancount_importer.beancount_io.reader import _coerce_date
        assert _coerce_date("2024-01-15") == date(2024, 1, 15)

    def test_quoted_iso_string(self):
        from beancount_importer.beancount_io.reader import _coerce_date
        # Beancount preserves quotes around metadata strings. Strip them
        # before parsing.
        assert _coerce_date('"2024-01-15"') == date(2024, 1, 15)

    def test_dotted_string(self):
        from beancount_importer.beancount_io.reader import _coerce_date
        assert _coerce_date("15.01.2024") == date(2024, 1, 15)

    def test_slashed_string(self):
        from beancount_importer.beancount_io.reader import _coerce_date
        assert _coerce_date("15/01/2024") == date(2024, 1, 15)

    def test_garbage_string_returns_none(self):
        from beancount_importer.beancount_io.reader import _coerce_date
        assert _coerce_date("not-a-date") is None

    def test_non_string_non_date_returns_none(self):
        from beancount_importer.beancount_io.reader import _coerce_date
        # Integers and other non-date-like values should fall through to None
        # rather than raise.
        assert _coerce_date(12345) is None
        assert _coerce_date(None) is None


# ── writer: format_transaction ───────────────────────────────────────────────

class TestFormatTransaction:
    def test_basic_format(self):
        text = format_transaction(
            date_str="2024-01-15",
            flag="*",
            payee="Netflix",
            narration="Netflix Abo",
            postings=[
                ("Assets:B:SPK", "-15.99 EUR"),
                ("Expenses:Entertainment", "15.99 EUR"),
            ],
        )
        assert "2024-01-15 * " in text
        assert '"Netflix"' in text
        assert '"Netflix Abo"' in text
        assert "Assets:B:SPK" in text
        assert "-15.99 EUR" in text

    def test_no_payee(self):
        text = format_transaction(
            date_str="2024-01-15",
            flag="*",
            payee=None,
            narration="Transfer",
            postings=[("Assets:B:SPK", "-100 EUR"), ("Assets:B:N26", "100 EUR")],
        )
        assert '"Transfer"' in text
        # No extra quotes for missing payee
        assert text.count('"Transfer"') == 1

    def test_metadata_included(self):
        text = format_transaction(
            date_str="2024-01-15",
            flag="*",
            payee=None,
            narration="Test",
            postings=[("Assets:B:SPK", "-1 EUR"), ("Expenses:Other", "1 EUR")],
            metadata={"sepa_ref": "REF-001"},
        )
        assert 'sepa_ref: "REF-001"' in text

    def test_ends_with_newline(self):
        text = format_transaction(
            date_str="2024-01-15",
            flag="*",
            payee=None,
            narration="Test",
            postings=[("Assets:B:SPK", "-1 EUR"), ("Expenses:Other", "1 EUR")],
        )
        assert text.endswith("\n")


# ── writer: append_entry ─────────────────────────────────────────────────────

class TestAppendEntry:
    def test_appends_to_new_file(self, tmp_path: Path):
        target = tmp_path / "SPK.bean"
        append_entry("2024-01-15 * \"Netflix\" \"test\"\n  Assets:B:SPK  -15 EUR\n", target)
        assert target.exists()
        assert "Netflix" in target.read_text()

    def test_dry_run_does_not_write(self, tmp_path: Path):
        target = tmp_path / "SPK.bean"
        append_entry("...", target, dry_run=True)
        assert not target.exists()

    def test_appends_to_existing(self, tmp_path: Path):
        target = tmp_path / "SPK.bean"
        target.write_text("first entry\n")
        append_entry("second entry\n", target)
        content = target.read_text()
        assert "first entry" in content
        assert "second entry" in content

    def test_creates_parent_dirs(self, tmp_path: Path):
        target = tmp_path / "sub" / "dir" / "SPK.bean"
        append_entry("entry\n", target)
        assert target.exists()


class TestSpliceEntries:
    """Cover the back-to-front splicing invariant called out in CLAUDE.md.

    Splicing front-to-back would shift the line numbers of every subsequent
    splice, corrupting the file. The writer sorts splices by `line_start`
    descending so that earlier-line splices stay accurate even after later
    ones replace differently-sized blocks. Subprocess calls to `bean-check`
    are mocked here — we don't need the binary to validate the logic.
    """

    def _stub_bean_check(self, monkeypatch, returncode: int = 0, stderr: str = ""):
        """Replace `subprocess.run` so bean-check returns the requested status
        without actually shelling out."""
        from beancount_importer.beancount_io import writer as writer_mod

        class _Result:
            def __init__(self):
                self.returncode = returncode
                self.stderr = stderr

        monkeypatch.setattr(
            writer_mod.subprocess, "run", lambda *a, **kw: _Result()
        )

    def test_splices_apply_back_to_front(self, tmp_path: Path, monkeypatch):
        # Replace two non-overlapping single-line ranges where the EARLIER
        # replacement is multi-line and the LATER one is single-line. If
        # splicing went front-to-back, the L2→[A1,A2] replacement would shift
        # L5 down by one line, and the (5,5) splice would corrupt L6 instead.
        # Sorting back-to-front sidesteps this.
        target = tmp_path / "x.bean"
        target.write_text(
            "L1\n"
            "L2\n"
            "L3\n"
            "L4\n"
            "L5\n"
            "L6\n"
        )
        self._stub_bean_check(monkeypatch)
        splice_entries(
            [
                # (start, end) ranges are 1-based inclusive — `start` and
                # `end` equal selects exactly one line.
                (2, 2, "A1\nA2"),  # L2 → two lines
                (5, 5, "B1"),       # L5 → one line
            ],
            target,
        )
        content = target.read_text().splitlines()
        # L2 expanded to two lines; L5 replaced by one line; everything else intact.
        assert content == ["L1", "A1", "A2", "L3", "L4", "B1", "L6"]

    def test_dry_run_does_not_modify(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "x.bean"
        target.write_text("hello\n")
        # Even with a stubbed bean-check, dry_run should short-circuit
        # before touching the file.
        called = []
        from beancount_importer.beancount_io import writer as writer_mod
        monkeypatch.setattr(
            writer_mod.subprocess,
            "run",
            lambda *a, **kw: called.append(a) or None,
        )
        splice_entries([(1, 1, "OOPS")], target, dry_run=True)
        assert target.read_text() == "hello\n"
        assert called == []  # bean-check not invoked

    def test_empty_updates_is_noop(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "x.bean"
        target.write_text("hello\n")
        called = []
        from beancount_importer.beancount_io import writer as writer_mod
        monkeypatch.setattr(
            writer_mod.subprocess,
            "run",
            lambda *a, **kw: called.append(a) or None,
        )
        splice_entries([], target)
        assert target.read_text() == "hello\n"
        # No bean-check needed when there's nothing to splice.
        assert called == []

    def test_bean_check_failure_rolls_back(self, tmp_path: Path, monkeypatch):
        # Critical: if bean-check rejects the post-splice file, the writer
        # restores the .bak and raises so the user's ledger is never left
        # in a broken state.
        import pytest

        target = tmp_path / "x.bean"
        original = "L1\nL2\nL3\n"
        target.write_text(original)
        self._stub_bean_check(monkeypatch, returncode=1, stderr="syntax error")
        with pytest.raises(RuntimeError, match="bean-check failed"):
            splice_entries([(2, 2, "REPLACED")], target)
        # File contents must equal the pre-splice state.
        assert target.read_text() == original
        # Backup file must have been cleaned up after rollback.
        assert not target.with_suffix(target.suffix + ".bak").exists()

    def test_successful_splice_removes_backup(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "x.bean"
        target.write_text("L1\nL2\nL3\n")
        self._stub_bean_check(monkeypatch)
        splice_entries([(2, 2, "REPLACED")], target)
        # Backup is cleaned up on success.
        assert not target.with_suffix(target.suffix + ".bak").exists()
