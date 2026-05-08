from datetime import date
from decimal import Decimal
from pathlib import Path

from beancount_importer.models import (
    CategoryProposal,
    ImportResult,
    LedgerEntry,
    Posting,
    SourceTransaction,
)
from beancount_importer.replay import (
    DecisionLog,
    make_decision_signature,
)
from beancount_importer.rules.models import CategorizationRule


def make_txn(**kwargs) -> SourceTransaction:
    defaults = dict(
        booking_date=date(2024, 1, 15),
        amount=Decimal("-15.99"),
        currency="EUR",
        bank_key="spk",
    )
    return SourceTransaction(**(defaults | kwargs))


def make_proposal(account: str = "Expenses:Foo", **kwargs) -> CategoryProposal:
    defaults = dict(
        action="categorize",
        postings=(Posting(account=account),),
    )
    return CategoryProposal(**(defaults | kwargs))


def make_result(
    txn: SourceTransaction,
    *,
    action: str = "new",
    proposal: CategoryProposal | None = None,
    is_replay: bool = False,
    rule_matched: CategorizationRule | None = None,
    new_rule: CategorizationRule | None = None,
    matched_entry: LedgerEntry | None = None,
) -> ImportResult:
    return ImportResult(
        source_txn=txn,
        action=action,  # type: ignore[arg-type]
        proposal=proposal if proposal is not None else make_proposal(),
        is_replay=is_replay,
        rule_matched=rule_matched,
        new_rule=new_rule,
        matched_entry=matched_entry,
    )


# ── DecisionSignature ─────────────────────────────────────────────────────────

class TestDecisionSignature:
    def test_sepa_present_uses_sepa_key(self):
        sig = make_decision_signature(make_txn(sepa_reference="REF-001"))
        assert sig.sepa_ref == "REF-001"
        assert sig.as_key() == "sepa:REF-001"

    def test_no_sepa_uses_hash_key(self):
        sig = make_decision_signature(make_txn(sepa_reference=""))
        assert sig.sepa_ref is None
        assert sig.as_key().startswith("hash:")

    def test_signature_stable_across_normalization(self):
        sig1 = make_decision_signature(make_txn(payee="Müller", description="Café"))
        sig2 = make_decision_signature(make_txn(payee="muller", description="cafe"))
        assert sig1.content_hash == sig2.content_hash

    def test_different_amounts_differ(self):
        a = make_decision_signature(make_txn(sepa_reference="", amount=Decimal("-10")))
        b = make_decision_signature(make_txn(sepa_reference="", amount=Decimal("-20")))
        assert a.content_hash != b.content_hash


# ── DecisionLog: lookup ───────────────────────────────────────────────────────

class TestDecisionLogLookup:
    def test_returns_none_when_no_path(self):
        log = DecisionLog(None)
        assert log.lookup(make_txn()) is None

    def test_returns_none_for_unknown(self, tmp_path: Path):
        log = DecisionLog(tmp_path / "decisions.jsonl")
        assert log.lookup(make_txn()) is None

    def test_returns_recorded_proposal(self, tmp_path: Path):
        log_path = tmp_path / "decisions.jsonl"
        log = DecisionLog(log_path)
        txn = make_txn(sepa_reference="NETFLIX-001")
        proposal = make_proposal(account="Expenses:Streaming", payee="Netflix")
        log.record(txn, make_result(txn, proposal=proposal))
        log.flush()  # CLI does this on natural completion / [q]

        # Reopen to verify persistence
        reloaded = DecisionLog(log_path)
        retrieved = reloaded.lookup(txn)
        assert retrieved is not None
        assert retrieved.target_account == "Expenses:Streaming"
        assert retrieved.payee == "Netflix"


# ── DecisionLog: record gating ────────────────────────────────────────────────

class TestDecisionLogRecord:
    def test_skips_when_no_proposal(self, tmp_path: Path):
        log = DecisionLog(tmp_path / "decisions.jsonl")
        result = ImportResult(source_txn=make_txn(), action="skip", proposal=None)
        log.record(make_txn(), result)
        assert log.lookup(make_txn()) is None

    def test_skips_replayed(self, tmp_path: Path):
        log_path = tmp_path / "decisions.jsonl"
        log = DecisionLog(log_path)
        txn = make_txn(sepa_reference="X")
        log.record(txn, make_result(txn, is_replay=True))
        assert not log_path.exists() or log_path.read_text() == ""

    def test_skips_skip_action(self, tmp_path: Path):
        log_path = tmp_path / "decisions.jsonl"
        log = DecisionLog(log_path)
        txn = make_txn(sepa_reference="X")
        log.record(txn, make_result(txn, action="skip"))
        assert not log_path.exists() or log_path.read_text() == ""

    def test_skips_rule_driven(self, tmp_path: Path):
        log_path = tmp_path / "decisions.jsonl"
        log = DecisionLog(log_path)
        txn = make_txn(sepa_reference="X")
        rule = CategorizationRule(target_account="Expenses:Auto")
        log.record(txn, make_result(txn, rule_matched=rule))
        assert log.lookup(txn) is None

    def test_records_new_rule_creation(self, tmp_path: Path):
        log_path = tmp_path / "decisions.jsonl"
        log = DecisionLog(log_path)
        txn = make_txn(sepa_reference="X")
        rule = CategorizationRule(target_account="Expenses:Auto")
        # rule_matched + new_rule both set: the user accepted a rule match AND
        # asked to save it as a (new/updated) rule. We DO record this so re-imports
        # can replay the choice even if rules.json is later deleted.
        log.record(txn, make_result(txn, rule_matched=rule, new_rule=rule))
        assert log.lookup(txn) is not None

    def test_records_manual_decision(self, tmp_path: Path):
        log_path = tmp_path / "decisions.jsonl"
        log = DecisionLog(log_path)
        txn = make_txn(sepa_reference="X")
        log.record(txn, make_result(txn))
        assert log.lookup(txn) is not None

    def test_independent_of_bean_check(self, tmp_path: Path):
        """Once flushed, decisions persist even if a later write/check
        fails. The CLI flushes BEFORE writing .bean files exactly so
        manual choices survive any subsequent bean-check failure.
        """
        log_path = tmp_path / "decisions.jsonl"
        log = DecisionLog(log_path)
        txn = make_txn(sepa_reference="A")
        log.record(txn, make_result(txn))
        log.flush()  # the CLI's "preserve manual work" point
        # Simulate a downstream bean-check failure: nothing past the
        # flush call mutates the log, so the entry remains queryable.
        reloaded = DecisionLog(log_path)
        assert reloaded.lookup(txn) is not None


# ── DecisionLog: persistence and corruption tolerance ────────────────────────

class TestDecisionLogPersistence:
    def test_appends_one_line_per_record(self, tmp_path: Path):
        log_path = tmp_path / "d.jsonl"
        log = DecisionLog(log_path)
        log.record(make_txn(sepa_reference="A"), make_result(make_txn(sepa_reference="A")))
        log.record(make_txn(sepa_reference="B"), make_result(make_txn(sepa_reference="B")))
        log.flush()
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 2

    def test_corrupt_line_does_not_break_load(self, tmp_path: Path):
        log_path = tmp_path / "d.jsonl"
        log_path.write_text("{this is not valid json\n")
        # Should not raise
        log = DecisionLog(log_path)
        assert log.lookup(make_txn()) is None

    def test_creates_parent_dir(self, tmp_path: Path):
        log_path = tmp_path / "nested" / "deeper" / "d.jsonl"
        log = DecisionLog(log_path)
        txn = make_txn(sepa_reference="X")
        log.record(txn, make_result(txn))
        log.flush()
        assert log_path.exists()

    def test_blank_lines_skipped_on_load(self, tmp_path: Path):
        # A blank line between records (e.g., an editor inserted a stray newline)
        # must not cause a parse error or erase the surrounding entries.
        log_path = tmp_path / "d.jsonl"
        log_path.write_text(
            '{"sig": {"sepa_ref": "X"}, "decision": '
            '{"action": "categorize", "postings": [{"account": "Expenses:X"}]}}\n'
            '\n'  # blank line
            '   \n'  # whitespace-only line
            '{"sig": {"sepa_ref": "Y"}, "decision": '
            '{"action": "categorize", "postings": [{"account": "Expenses:Y"}]}}\n'
        )
        log = DecisionLog(log_path)
        assert log.lookup(make_txn(sepa_reference="X")) is not None
        assert log.lookup(make_txn(sepa_reference="Y")) is not None

    def test_record_without_decision_skipped_on_load(self, tmp_path: Path):
        # A line with a `sig` but no `decision` (truncated write?) shouldn't
        # be loaded into the index.
        log_path = tmp_path / "d.jsonl"
        log_path.write_text('{"sig": {"sepa_ref": "X"}}\n')
        log = DecisionLog(log_path)
        assert log.lookup(make_txn(sepa_reference="X")) is None

    def test_hash_keyed_record_loads(self, tmp_path: Path):
        # Cover the `else` branch of the sepa_ref ternary in _load.
        from beancount_importer.replay import make_decision_signature

        txn = make_txn(sepa_reference="")
        sig = make_decision_signature(txn)
        log_path = tmp_path / "d.jsonl"
        log_path.write_text(
            f'{{"sig": {{"hash": "{sig.content_hash}"}}, "decision": '
            '{"action": "categorize", "postings": [{"account": "Expenses:Hashed"}]}}\n'
        )
        log = DecisionLog(log_path)
        retrieved = log.lookup(txn)
        assert retrieved is not None
        assert retrieved.target_account == "Expenses:Hashed"


class TestDecisionLogFlush:
    """`flush()` is now the only path that writes JSONL. `record()`
    just buffers in memory; the CLI calls `flush()` on natural
    completion or `[q] quit`. Ctrl+C drops the buffer with the process.
    """

    def test_record_does_not_write_to_disk(self, tmp_path: Path):
        log_path = tmp_path / "d.jsonl"
        log = DecisionLog(log_path)
        txn = make_txn(sepa_reference="REF")
        log.record(txn, make_result(txn))
        # File must not exist yet — flush is what creates it.
        assert not log_path.exists()

    def test_flush_writes_buffered_records(self, tmp_path: Path):
        log_path = tmp_path / "d.jsonl"
        log = DecisionLog(log_path)
        txn1 = make_txn(sepa_reference="REF-1")
        txn2 = make_txn(sepa_reference="REF-2")
        log.record(txn1, make_result(txn1))
        log.record(txn2, make_result(txn2))
        assert log.flush() == 2
        assert log_path.exists()
        lines = log_path.read_text().splitlines()
        assert len(lines) == 2

    def test_flush_returns_zero_when_nothing_buffered(self, tmp_path: Path):
        log = DecisionLog(tmp_path / "d.jsonl")
        assert log.flush() == 0

    def test_flush_no_op_with_path_none(self):
        log = DecisionLog(None)
        txn = make_txn(sepa_reference="REF")
        log.record(txn, make_result(txn))
        assert log.flush() == 0

    def test_flush_clears_buffer(self, tmp_path: Path):
        # A second flush after a successful one writes nothing extra;
        # the buffer is consumed exactly once.
        log_path = tmp_path / "d.jsonl"
        log = DecisionLog(log_path)
        txn = make_txn(sepa_reference="REF")
        log.record(txn, make_result(txn))
        assert log.flush() == 1
        assert log.flush() == 0
        assert len(log_path.read_text().splitlines()) == 1

    def test_lookup_works_before_flush(self, tmp_path: Path):
        # The in-memory index is updated on `record()` (not gated on
        # flush) so a duplicate row processed later in the same run
        # still replays cleanly.
        log = DecisionLog(tmp_path / "d.jsonl")
        txn = make_txn(sepa_reference="REF")
        log.record(txn, make_result(txn))
        assert log.lookup(txn) is not None

    def test_dropped_buffer_means_nothing_persisted(self, tmp_path: Path):
        # Simulate Ctrl+C: record but never flush. Reloading from disk
        # sees zero records.
        log_path = tmp_path / "d.jsonl"
        log = DecisionLog(log_path)
        txn = make_txn(sepa_reference="REF")
        log.record(txn, make_result(txn))
        # Drop the in-process state by constructing a fresh log.
        del log
        fresh = DecisionLog(log_path)
        assert fresh.lookup(txn) is None
