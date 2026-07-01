"""Property-based invariants for the writer/persist path.

The 2024 corruption incident (sequential splices applied top-down with line
numbers captured at load time) motivates pinning these as invariants over
*generated* ledgers rather than single examples:

- persisting any mix of updates rewrites exactly the targeted transactions
  and leaves every other transaction intact
- a stale `line_start` (any nonzero shift) must never modify the file
- `format_transaction` output reparses to the same transaction, including
  `#tags` and `^links`
"""

from __future__ import annotations

import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from beancount.core import data as bc_data
from beancount.parser import parser as bc_parser
from hypothesis import given, settings
from hypothesis import strategies as st

from beancount_importer.beancount_io.reader import read_ledger
from beancount_importer.beancount_io.writer import apply_update, format_transaction
from beancount_importer.cli import _persist_results
from beancount_importer.config import BankConfig, Config, CsvConfig, MatchingConfig
from beancount_importer.models import (
    CategoryProposal,
    ImportResult,
    LedgerEntry,
    Posting,
    ProposedChange,
    SourceTransaction,
)

ACCOUNT = "Assets:B:SPK"

_names = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8)


@st.composite
def _ledgers(draw) -> list[dict]:
    """2-5 simple transactions with distinct dates, one expense leg each, and
    0-2 metadata lines so entry bodies vary in line count — variation in body
    height is exactly what made sequential splices drift."""
    n = draw(st.integers(min_value=2, max_value=5))
    return [
        {
            "date": date(2024, 1, 1) + timedelta(days=i),
            "payee": draw(_names),
            "amount": Decimal(draw(st.integers(min_value=1, max_value=99999))) / 100,
            "meta_lines": draw(st.integers(min_value=0, max_value=2)),
            "old_account": f"Expenses:Old{i}",
        }
        for i in range(n)
    ]


def _render(txns: list[dict]) -> str:
    blocks = []
    for t in txns:
        lines = [f'{t["date"].isoformat()} * "{t["payee"]}" "orig"']
        lines += [f'  note{j}: "m{j}"' for j in range(t["meta_lines"])]
        lines.append(f'  {ACCOUNT}  -{t["amount"]} EUR')
        lines.append(f'  {t["old_account"]}  {t["amount"]} EUR')
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def _config() -> Config:
    return Config(
        banks=[
            BankConfig(
                key="spk",
                display_name="Sparkasse",
                account=ACCOUNT,
                file_glob="SPK_*.csv",
                output_file="SPK.bean",
                csv=CsvConfig(
                    delimiter=";",
                    date_format=["%d.%m.%y"],
                    amount_locale="de",
                    field_date="Buchungstag",
                    field_amount="Betrag",
                ),
            )
        ],
        matching=MatchingConfig(min_score=0.35),
    )


def _update_result(entry: LedgerEntry, new_account: str) -> ImportResult:
    return ImportResult(
        source_txn=SourceTransaction(
            booking_date=entry.date, amount=entry.amount, bank_key="spk"
        ),
        action="update",
        matched_entry=entry,
        proposal=CategoryProposal(
            action="categorize",
            postings=(Posting(account=new_account),),
            # Extra metadata line: every replacement is taller than its
            # original, so any stale-coordinate bug must shift the file.
            metadata={"recat": "yes"},
        ),
        proposed_changes=[
            ProposedChange("target_account", entry.target_account, new_account)
        ],
    )


@settings(max_examples=25, deadline=None)
@given(data=st.data())
def test_persist_rewrites_targets_and_preserves_the_rest(data):
    """For any ledger and any non-empty subset of its entries updated (in
    top-down results order), the persisted file must contain exactly the
    original transactions — targeted ones recategorized, the rest unchanged.
    """
    txns = data.draw(_ledgers())
    mask = data.draw(
        st.lists(
            st.booleans(), min_size=len(txns), max_size=len(txns)
        ).filter(any)
    )
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "SPK.bean"
        f.write_text(_render(txns))
        entries = sorted(read_ledger(f, ACCOUNT), key=lambda e: e.line_start)
        assert len(entries) == len(txns)

        results = [
            _update_result(e, f"Expenses:New{i}")
            for i, (e, m) in enumerate(zip(entries, mask, strict=True))
            if m
        ]
        failures = _persist_results(results, _config(), Path(td), dry_run=False)
        assert failures == []

        after = read_ledger(f, ACCOUNT)
        got = {(e.date, e.amount, e.target_account) for e in after}
        want = {
            (
                t["date"],
                -t["amount"],
                f"Expenses:New{i}" if m else t["old_account"],
            )
            for i, (t, m) in enumerate(zip(txns, mask, strict=True))
        }
        assert got == want
        # Payees survive on updated and untouched entries alike.
        assert {(e.date, e.payee) for e in after} == {
            (t["date"], t["payee"]) for t in txns
        }


@settings(max_examples=25, deadline=None)
@given(data=st.data())
def test_stale_line_start_never_modifies_the_file(data):
    """Shifting any entry's line_start by any nonzero amount must make
    apply_update refuse (ValueError) and leave the file byte-identical —
    a coordinate bug degrades to a clean per-entry failure, never corruption.
    """
    txns = data.draw(_ledgers())
    idx = data.draw(st.integers(min_value=0, max_value=len(txns) - 1))
    shift = data.draw(
        st.integers(min_value=-4, max_value=4).filter(lambda s: s != 0)
    )
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "SPK.bean"
        f.write_text(_render(txns))
        entries = sorted(read_ledger(f, ACCOUNT), key=lambda e: e.line_start)
        stale = entries[idx].model_copy(
            update={"line_start": entries[idx].line_start + shift}
        )
        original = f.read_text()
        proposal = CategoryProposal(
            action="categorize", postings=(Posting(account="Expenses:X"),)
        )
        with pytest.raises(ValueError, match="mismatch"):
            apply_update(stale, proposal, ACCOUNT)
        assert f.read_text() == original


_tag_names = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=10
).filter(lambda s: not s.startswith("-"))


@settings(deadline=None)
@given(
    payee=_names,
    narration=st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", max_size=20),
    tags=st.lists(_tag_names, max_size=3, unique=True),
    links=st.lists(_tag_names, max_size=3, unique=True),
)
def test_format_transaction_reparses_to_same_transaction(
    payee, narration, tags, links
):
    text = format_transaction(
        date_str="2024-03-05",
        flag="*",
        payee=payee,
        narration=narration,
        postings=[(ACCOUNT, "-12.34 EUR"), ("Expenses:X", None)],
        tags=tags,
        links=links,
    )
    entries, errors, _ = bc_parser.parse_string(text)
    assert not errors, errors
    txn = entries[0]
    assert isinstance(txn, bc_data.Transaction)
    assert txn.date == date(2024, 3, 5)
    assert txn.payee == payee
    assert txn.narration == narration
    assert set(txn.tags or ()) == set(tags)
    assert set(txn.links or ()) == set(links)
