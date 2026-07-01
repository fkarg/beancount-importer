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

    def __init__(
        self,
        path: Path | None,
        session_id: str | None = None,
        placeholder_account: str = "Expenses:Unknown",
    ) -> None:
        self.path = path
        self.session_id = session_id or _new_session_id()
        self.placeholder_account = placeholder_account
        self._index: dict[str, CategoryProposal] = {}
        # Full loaded records (keyed by signature) — kept so `flush()` can
        # rewrite the file, dropping decisions superseded this run.
        self._records: dict[str, dict[str, Any]] = {}
        # New one-off records this run, and keys to drop on the next flush.
        self._pending: list[dict[str, Any]] = []
        self._superseded: set[str] = set()
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
                decision = entry.get("decision")
                if decision is None:
                    continue
                key = _record_key(entry.get("sig", {}))
                self._records[key] = entry  # last line wins (dedups the file)
                self._index[key] = _deserialize_proposal(decision)

    def lookup(self, txn: SourceTransaction) -> CategoryProposal | None:
        sig = make_decision_signature(txn)
        return self._index.get(sig.as_key())

    def record(self, txn: SourceTransaction, result: ImportResult) -> None:
        """Buffer the result's decision in memory for later `flush()`.

        Skips:
        - results without a proposal (e.g. dedup-skipped, transfer-linked)
        - replayed decisions (no circular recording)
        - non-user-driven skip/quit actions (`duplicate`, `skip_rule`,
          `cross_source_match` — all deterministic from current state
          and don't need replay support). The user-driven skip variants
          (`user_skipped`, `user_blocked`) flow through `_apply_merge_decision`
          which already clears `proposal`, so they're caught by the
          `proposal is None` guard above.
        - any rule-involved decision (matched or newly created) — the rule is
          the durable record; a replay entry would only shadow it
        - placeholder decisions (target == the configured placeholder account),
          which are "not decided yet", not one-offs worth persisting

        Silent-match updates (`update` action with no proposed_changes)
        ARE recorded — the seed-silent-skip path produced a proposal
        the user effectively consented to (Screen 3 'keep') or that
        followed from the seed-equals-entry case; recording lets the
        next run replay the same outcome without re-running the
        scorer or prompting again.

        Also self-cleans: if a stored decision exists for this txn but the txn
        is now handled by the ledger (dedup), a cross-source match, or a rule
        — or was reset to the placeholder — that stored decision is superseded
        and dropped on the next `flush()`.

        The in-memory `_index` is updated immediately so subsequent `lookup()`
        calls within the same run see the change. Disk write is deferred to
        `flush()`.
        """
        if self.path is None:
            return
        key = make_decision_signature(txn).as_key()
        if result.is_replay:
            return  # the decision was used — keep it
        if _is_one_off(result, self.placeholder_account):
            assert result.proposal is not None
            sig = make_decision_signature(txn)
            self._pending.append({
                "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "session": self.session_id,
                "bank": txn.bank_key,
                # Human-readable context so decisions.jsonl can be scanned and
                # hand-edited; ignored on load (matching keys off `sig`).
                "date": txn.booking_date.isoformat(),
                "payee": txn.payee or txn.description or "",
                "amount": str(txn.amount),
                "target": result.proposal.target_account,
                "sig": {"sepa_ref": sig.sepa_ref, "hash": sig.content_hash},
                "decision": _serialize_proposal(result.proposal),
            })
            self._index[key] = result.proposal
            self._superseded.discard(key)
            return
        if key in self._index and _supersedes_decision(result, self.placeholder_account):
            self._index.pop(key, None)
            self._superseded.add(key)

    def flush(self) -> int:
        """Rewrite the JSONL: loaded records minus superseded, plus new ones.

        Returns the count of new one-offs recorded this run. Called by the CLI
        on a successful session (natural completion or `[q] quit`); on Ctrl+C
        the caller skips it, so the buffered changes fall away with the process.

        Rewrite (not append) so superseded decisions can be dropped and
        duplicate signatures collapsed. The file is left untouched when nothing
        changed, so a no-op run produces no spurious diff.
        """
        if self.path is None or (not self._pending and not self._superseded):
            return 0
        merged = {
            k: rec for k, rec in self._records.items() if k not in self._superseded
        }
        for rec in self._pending:
            merged[_record_key(rec["sig"])] = rec  # new wins, keeps stable order
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as fh:
            for rec in merged.values():
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        count = len(self._pending)
        self._records = merged
        self._pending.clear()
        self._superseded.clear()
        return count


def _record_key(sig: dict[str, Any]) -> str:
    return (
        f"sepa:{sig['sepa_ref']}"
        if sig.get("sepa_ref")
        else f"hash:{sig.get('hash', '')}"
    )


def _is_one_off(result: ImportResult, placeholder_account: str) -> bool:
    """A genuine one-off worth persisting: a real category the user chose for
    this specific txn, with no rule involved and not a placeholder."""
    if result.proposal is None or result.action in ("skip", "quit"):
        return False
    if result.rule_matched is not None or result.new_rule is not None:
        return False
    return result.proposal.target_account != placeholder_account


def _supersedes_decision(result: ImportResult, placeholder_account: str) -> bool:
    """Whether this outcome makes a stored decision obsolete — the txn is now
    handled authoritatively by the ledger, a cross-source match, or a rule, or
    was reset to the placeholder. A plain user skip/quit leaves it alone."""
    if result.rule_matched is not None or result.new_rule is not None:
        return True
    if result.skip_reason in ("duplicate", "cross_source_match"):
        return True
    return (
        result.proposal is not None
        and result.proposal.target_account == placeholder_account
    )


def _new_session_id() -> str:
    """Short random identifier used to group decisions by import session."""
    return hashlib.sha256(os.urandom(16)).hexdigest()[:12]
