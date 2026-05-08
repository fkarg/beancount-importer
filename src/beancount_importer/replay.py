"""Decision log: persist non-rule, user-driven categorization choices for replay.

Why this exists: rules capture *patterns* of decisions, but some categorizations
are intrinsically one-off — "this specific transaction is a gift, just this once".
Without persistence, re-importing a year later forces the user to re-decide every
such case. The decision log captures them so subsequent runs replay automatically.

Write ordering: decisions are **buffered in memory** during the run and flushed
to JSONL only when the user accepts them (a successful natural completion or
`[q] quit`). Ctrl+C simply doesn't flush — the partial work disappears.
This honours the `[q] saves, Ctrl+C does not` contract end-to-end.

Earlier versions wrote per-decision in-flight to survive crashes, but the
trade-off backfired: any interrupted run silently persisted half the
session as confirmed, contradicting the user's "abandon" intent. A crash
mid-run now loses the session's decisions; that's the price for honest
Ctrl+C semantics.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from beancount_importer.matching.normalize import normalize_text
from beancount_importer.models import (
    CategoryProposal,
    ImportResult,
    Posting,
    SourceTransaction,
)


class DecisionSignature(BaseModel):
    """Identity of a decision keyed off the source transaction.

    Same strategy as deduplication: SEPA reference is the primary key when
    present (banks guarantee its uniqueness for SEPA payments), otherwise a
    content hash over normalized text + amount + date.
    """

    model_config = ConfigDict(frozen=True)

    sepa_ref: str | None
    content_hash: str

    def as_key(self) -> str:
        return f"sepa:{self.sepa_ref}" if self.sepa_ref else f"hash:{self.content_hash}"


def make_decision_signature(txn: SourceTransaction) -> DecisionSignature:
    sepa = txn.sepa_reference or None
    parts = "|".join([
        str(txn.booking_date),
        str(txn.amount),
        txn.currency,
        normalize_text((txn.payee or "")[:50]),
        normalize_text((txn.description or "")[:100]),
    ])
    digest = hashlib.sha256(parts.encode()).hexdigest()[:16]
    return DecisionSignature(sepa_ref=sepa, content_hash=digest)


def _serialize_proposal(proposal: CategoryProposal) -> dict[str, Any]:
    """Strip non-portable fields (rule_used) and serialize to a JSON-able dict."""
    postings_data = [
        {
            "account": p.account,
            "amount": str(p.amount) if p.amount is not None else None,
            "currency": p.currency,
            "metadata": p.metadata,
        }
        for p in proposal.postings
    ]
    return {
        "action": proposal.action,
        "postings": postings_data,
        "payee": proposal.payee,
        "narration": proposal.narration,
        "metadata": proposal.metadata,
        "tag": proposal.tag,
        "save_as_rule": proposal.save_as_rule,
    }


def _deserialize_proposal(data: dict[str, Any]) -> CategoryProposal:
    postings = tuple(
        Posting(
            account=p["account"],
            amount=Decimal(p["amount"]) if p.get("amount") is not None else None,
            currency=p.get("currency"),
            metadata=p.get("metadata", {}),
        )
        for p in data.get("postings", [])
    )
    return CategoryProposal(
        action=data["action"],
        postings=postings,
        payee=data.get("payee"),
        narration=data.get("narration"),
        metadata=data.get("metadata", {}),
        tag=data.get("tag"),
        save_as_rule=data.get("save_as_rule", False),
    )


class DecisionLog:
    """Append-only JSONL log of past categorization decisions.

    Construction loads existing entries into an in-memory index keyed by
    `DecisionSignature`. `lookup()` is O(1); `record()` appends and updates
    the index without rereading the file.

    `path=None` produces a no-op log (useful for tests that don't care).
    """

    def __init__(self, path: Path | None, session_id: str | None = None) -> None:
        self.path = path
        self.session_id = session_id or _new_session_id()
        self._index: dict[str, CategoryProposal] = {}
        # Records accumulated during the run. `flush()` writes them; Ctrl+C
        # just drops the buffer.
        self._pending: list[dict[str, Any]] = []
        if path is not None and path.exists():
            self._load()

    def _load(self) -> None:
        assert self.path is not None
        with self.path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue  # ignore corrupt lines rather than fail the whole import
                sig = entry.get("sig", {})
                key = (
                    f"sepa:{sig['sepa_ref']}"
                    if sig.get("sepa_ref")
                    else f"hash:{sig.get('hash', '')}"
                )
                decision = entry.get("decision")
                if decision is None:
                    continue
                self._index[key] = _deserialize_proposal(decision)

    def lookup(self, txn: SourceTransaction) -> CategoryProposal | None:
        sig = make_decision_signature(txn)
        return self._index.get(sig.as_key())

    def record(self, txn: SourceTransaction, result: ImportResult) -> None:
        """Buffer the result's decision in memory for later `flush()`.

        Skips:
        - results without a proposal (e.g. dedup-skipped, transfer-linked)
        - replayed decisions (no circular recording)
        - skip/quit actions
        - rule-driven decisions where the user didn't ask to save as a rule
          (the rule itself is the persistent record; replaying would shadow it)

        The in-memory `_index` is updated immediately so subsequent
        `lookup()` calls within the same run see the new decision (e.g.
        a duplicate row processed later in the same session replays
        cleanly rather than re-prompting). Only the disk write is
        deferred until `flush()`.
        """
        if self.path is None or result.proposal is None:
            return
        if result.is_replay or result.action in ("skip", "quit"):
            return
        if result.rule_matched is not None and result.new_rule is None:
            return

        sig = make_decision_signature(txn)
        record = {
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session": self.session_id,
            "bank": txn.bank_key,
            "sig": {"sepa_ref": sig.sepa_ref, "hash": sig.content_hash},
            "decision": _serialize_proposal(result.proposal),
        }

        self._pending.append(record)
        self._index[sig.as_key()] = result.proposal

    def flush(self) -> int:
        """Persist all pending records to the JSONL. Returns the count.

        Called by the CLI when the user signals the session was a
        success (natural completion or `[q] quit`). On Ctrl+C, the
        caller skips this — `_pending` falls away with the process,
        so no rollback step is needed.

        Append-only with fsync per batch. A single fsync amortises
        the durability cost across the whole session.
        """
        if self.path is None or not self._pending:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        count = len(self._pending)
        with self.path.open("a", encoding="utf-8") as fh:
            for record in self._pending:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._pending.clear()
        return count


def _new_session_id() -> str:
    """Short random identifier used to group decisions by import session."""
    return hashlib.sha256(os.urandom(16)).hexdigest()[:12]
