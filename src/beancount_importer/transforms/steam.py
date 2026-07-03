"""Steam game-name enrichment.

Steam bank/PayPal rows only carry the merchant "www.steampowered.com" plus an
amount — the actual game titles live in Steam's own purchase-history CSV export.
When a Steam purchase is categorized, this looks up its (booking date, amount)
in a prebuilt index of that CSV and, on a *unique* hit, rewrites the narration
to the titles and stores the full list in a `games:` metadata key. Ambiguous or
missing hits leave the proposal untouched — no guessing.

Unlike the other transforms this one is data-backed, so it is not a module-level
`hook`: `run()` builds a `SteamEnricher` from `load_steam_index(...)` when
`Config.steam_history_file` is set and appends it to the transform list.
"""

from __future__ import annotations

import csv
import html
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from beancount_importer.models import CategoryProposal, SourceTransaction
from beancount_importer.rules.models import CategorizationRule

_MERCHANT_TOKEN = "steampowered"

# (booking date, absolute amount) → the title lists of every CSV row with that
# key. More than one entry means an ambiguous key we refuse to enrich from.
SteamIndex = dict[tuple[date, Decimal], list[list[str]]]


def _parse_eur(raw: str) -> Decimal | None:
    """Parse a Steam "Total" cell (e.g. '65,20€', '32.--€') to a Decimal."""
    s = raw.replace("€", "").strip()
    if not s:
        return None
    s = s.replace(".--", "").replace(".", "").replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def load_steam_index(path: Path) -> SteamIndex:
    """Build a (date, amount) → title-lists index from a Steam history CSV."""
    index: SteamIndex = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            amount = _parse_eur(row.get("Total", ""))
            if amount is None:
                continue
            try:
                day = datetime.strptime(row.get("Date", "").strip(), "%d %b, %Y").date()
            except ValueError:
                continue
            titles = [
                html.unescape(t.strip())
                for t in row.get("Items", "").splitlines()
                if t.strip()
            ]
            if not titles:
                continue
            index.setdefault((day, amount), []).append(titles)
    return index


class SteamEnricher:
    name = "steam"

    def __init__(self, index: SteamIndex) -> None:
        self._index = index

    def applies_to(self, rule: CategorizationRule) -> bool:
        # Gating is on the transaction (merchant + unique index hit), done in
        # `apply`; every rule-matched txn is a candidate to inspect.
        return True

    def apply(
        self,
        proposal: CategoryProposal,
        txn: SourceTransaction,
        rule: CategorizationRule,
    ) -> CategoryProposal:
        haystack = f"{txn.payee or ''} {txn.description or ''}".lower()
        if _MERCHANT_TOKEN not in haystack:
            return proposal
        candidates = self._index.get((txn.booking_date, abs(txn.amount)))
        if candidates is None or len(candidates) != 1:
            return proposal
        titles = [t.replace('"', "'") for t in candidates[0]]
        full = ", ".join(titles)
        summary = full if len(titles) <= 2 else f"{titles[0]}, {titles[1]} (+{len(titles) - 2} more)"
        return proposal.model_copy(
            update={
                "narration": f"Steam: {summary}",
                "metadata": {**proposal.metadata, "games": full},
            }
        )
