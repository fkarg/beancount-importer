"""Pipeline applies `proposal.tag_state_delta` to mid-run tag state.

Verifies that a Screen-1 `[t]` choice (modeled here by a stub
`categorize_fn` that returns a proposal carrying a `tag_state_delta`)
flows through the run loop:

- The current txn's proposal gets stamped with the new tag.
- Subsequent txns inherit the active tag.
- The result's `tag_state_delta` reflects the user choice for
  persistence by `_persist_tag_updates` in cli.py.
"""

from __future__ import annotations

from pathlib import Path

from beancount_importer.config import (
    BankConfig,
    Config,
    CsvConfig,
    MatchingConfig,
)
from beancount_importer.models import (
    CategoryProposal,
    Posting,
)
from beancount_importer.pipeline import (
    CategorizeContext,
    NoopReporter,
    run,
)
from beancount_importer.rules.tags import ActiveTag, TagStateDelta
from beancount_importer.session import ImportOptions, ImportSession


def _spk_bank() -> BankConfig:
    return BankConfig(
        key="spk",
        display_name="Sparkasse",
        account="Assets:B:SPK",
        file_glob="SPK_*.csv",
        output_file="SPK.bean",
        csv=CsvConfig(
            delimiter=";",
            date_format=["%d.%m.%y"],
            amount_locale="de",
            field_date="Buchungstag",
            field_amount="Betrag",
            field_currency="Waehrung",
            field_payee="Beguenstigter",
            field_description="Verwendungszweck",
        ),
    )


def _three_txn_csv(tmp_path: Path) -> Path:
    csv = tmp_path / "SPK_2024.csv"
    csv.write_text(
        "Buchungstag;Beguenstigter;Verwendungszweck;Betrag;Waehrung\n"
        "01.03.24;A;a;-1,00;EUR\n"
        "02.03.24;B;b;-2,00;EUR\n"
        "03.03.24;C;c;-3,00;EUR\n"
    )
    (tmp_path / "transactions").mkdir()
    return tmp_path


def _session(base_dir: Path) -> ImportSession:
    cfg = Config(
        banks=[_spk_bank()],
        transactions_dir="transactions",
        matching=MatchingConfig(min_score=0.35),
    )
    return ImportSession(config=cfg, options=ImportOptions())


def _make_categorizer(deltas_by_index: dict[int, TagStateDelta]):
    """Stub `CategorizeFn` that injects a tag delta on the Nth call.

    Each invocation returns a categorize proposal targeting `Expenses:X`;
    if the call index appears in `deltas_by_index`, the proposal carries
    that `tag_state_delta`. Lets tests script "user pressed [t] on txn 1".
    """
    counter = {"n": 0}

    def _fn(ctx: CategorizeContext) -> CategoryProposal:
        i = counter["n"]
        counter["n"] += 1
        return CategoryProposal(
            action="categorize",
            postings=(Posting(account="Expenses:X"),),
            payee=ctx.txn.payee,
            narration=ctx.txn.description,
            tag_state_delta=deltas_by_index.get(i),
        )

    return _fn


# ── Always-mode tag set on first txn carries forward ─────────────────────────


class TestSetAlwaysOnFirstTxn:
    def test_first_and_subsequent_txns_get_tagged(self, tmp_path: Path):
        base = _three_txn_csv(tmp_path)
        delta = TagStateDelta(
            op="set", new_state=ActiveTag(tag="trip", mode="always")
        )
        results = run(
            _session(base),
            base,
            _make_categorizer({0: delta}),
            NoopReporter(),
        )
        assert len(results) == 3
        # All three got the proposal-level tag stamp via the pipeline's
        # auto-apply-active-tag step.
        for r in results:
            assert r.proposal is not None
            assert r.proposal.tag == "trip"

    def test_result_tag_state_delta_records_user_intent(self, tmp_path: Path):
        # The user's set-always delta surfaces on the first result so
        # `_persist_tag_updates` writes it out at end of run.
        base = _three_txn_csv(tmp_path)
        delta = TagStateDelta(
            op="set", new_state=ActiveTag(tag="trip", mode="always")
        )
        results = run(
            _session(base),
            base,
            _make_categorizer({0: delta}),
            NoopReporter(),
        )
        assert results[0].tag_state_delta is not None
        assert results[0].tag_state_delta.op == "set"
        assert results[0].tag_state_delta.new_state is not None
        assert results[0].tag_state_delta.new_state.tag == "trip"


# ── Once-mode tag applies only to the txn it was set on ──────────────────────


class TestSetOnce:
    def test_only_first_txn_gets_tagged(self, tmp_path: Path):
        base = _three_txn_csv(tmp_path)
        delta = TagStateDelta(
            op="set", new_state=ActiveTag(tag="hint", mode="once")
        )
        results = run(
            _session(base),
            base,
            _make_categorizer({0: delta}),
            NoopReporter(),
        )
        # First txn: tagged. Subsequent: untagged (once auto-clears).
        assert results[0].proposal is not None and results[0].proposal.tag == "hint"
        assert results[1].proposal is not None and results[1].proposal.tag is None
        assert results[2].proposal is not None and results[2].proposal.tag is None


# ── Clear-mode wipes the active tag ──────────────────────────────────────────


class TestNoop:
    def test_noop_delta_is_accepted_without_changing_state(self, tmp_path: Path):
        # `op="noop"` is the explicit "no change" delta; the pipeline must
        # not treat it as a clear or a set. (The screen modules return
        # `None` instead of a noop today, but the literal stays valid.)
        base = _three_txn_csv(tmp_path)
        deltas = {0: TagStateDelta(op="noop")}
        results = run(
            _session(base),
            base,
            _make_categorizer(deltas),
            NoopReporter(),
        )
        # No tag stamping happens because there's no active tag.
        for r in results:
            assert r.proposal is not None
            assert r.proposal.tag is None


class TestClear:
    def test_clear_after_always_stops_tagging(self, tmp_path: Path):
        base = _three_txn_csv(tmp_path)
        # Set always on txn 0; clear on txn 1; txn 2 inherits no tag.
        deltas = {
            0: TagStateDelta(
                op="set", new_state=ActiveTag(tag="trip", mode="always")
            ),
            1: TagStateDelta(op="clear"),
        }
        results = run(
            _session(base),
            base,
            _make_categorizer(deltas),
            NoopReporter(),
        )
        assert results[0].proposal is not None
        assert results[0].proposal.tag == "trip"
        # txn 1: cleared at the start of the txn's processing, so the
        # auto-stamp step doesn't fire.
        assert results[1].proposal is not None
        assert results[1].proposal.tag is None
        assert results[2].proposal is not None
        assert results[2].proposal.tag is None
