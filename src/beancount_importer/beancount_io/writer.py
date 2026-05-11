from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from beancount_importer.models import CategoryProposal, LedgerEntry


def append_entry(text: str, target: Path, dry_run: bool = False) -> None:
    """Append a formatted beancount entry to target file."""
    if dry_run:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as f:
        if target.stat().st_size > 0:
            f.write("\n")
        f.write(text.rstrip())
        f.write("\n")


def splice_entries(
    updates: list[tuple[int, int, str]],
    target: Path,
    dry_run: bool = False,
) -> None:
    """Replace line ranges in target file with new text.

    updates: list of (line_start, line_end, new_text) — 1-based line numbers.
    Applied back-to-front to preserve line offsets for earlier entries.
    """
    if dry_run or not updates:
        return

    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    bak = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, bak)

    # Back-to-front ordering preserves offsets of earlier slices
    for start, end, new_text in sorted(updates, key=lambda u: u[0], reverse=True):
        s = start - 1  # convert to 0-based
        e = end  # end is exclusive
        replacement = new_text.rstrip() + "\n"
        lines[s:e] = [replacement]

    target.write_text("".join(lines), encoding="utf-8")

    result = subprocess.run(
        ["bean-check", str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        shutil.copy2(bak, target)
        bak.unlink()
        raise RuntimeError(
            f"bean-check failed after splice — rolled back:\n{result.stderr}"
        )
    bak.unlink()


def apply_update(
    entry: LedgerEntry,
    proposal: CategoryProposal,
    bank_account: str,
    *,
    dry_run: bool = False,
    narration_max_length: int | None = None,
) -> None:
    """Splice `entry` in place with a transaction reflecting `proposal`.

    `entry.line_start` pins the splice; the end of the transaction is detected
    by scanning the file for the first blank line (or next top-level directive)
    after the start. The bank-side leg always carries the original amount; any
    additional postings come from the proposal. Metadata merges entry-side
    metadata with proposal metadata (proposal wins) and includes `tag` when set.
    """
    payee = proposal.payee or entry.payee
    narration = proposal.narration or entry.narration
    postings: list[tuple[str, str | None, dict[str, str]]] = [
        (bank_account, f"{entry.amount} {entry.currency}", {})
    ]
    for p in proposal.postings:
        currency = p.currency or entry.currency
        amount_str = f"{p.amount} {currency}" if p.amount is not None else None
        postings.append((p.account, amount_str, dict(p.metadata)))
    metadata = {**entry.metadata, **proposal.metadata}
    if proposal.tag:
        metadata["tag"] = proposal.tag

    text = format_transaction(
        date_str=entry.date.isoformat(),
        flag=entry.flag,
        payee=payee,
        narration=narration,
        postings=postings,
        metadata=metadata,
        narration_max_length=narration_max_length,
    )
    target = Path(entry.file_path)
    line_end = entry.line_end or _detect_entry_end(target, entry.line_start)
    splice_entries([(entry.line_start, line_end, text)], target, dry_run=dry_run)


def _detect_entry_end(target: Path, line_start: int) -> int:
    """Find the 1-based last line of the transaction starting at `line_start`.

    Beancount's loader only records the start line. The end is whatever comes
    before the next blank line or top-level directive — postings and metadata
    are indented, so we stop at the first un-indented line after the header.
    """
    lines = target.read_text(encoding="utf-8").splitlines()
    end = len(lines)
    # Header is at index line_start - 1 (1-based → 0-based). Scan from the
    # next line forward.
    for i in range(line_start, len(lines)):
        line = lines[i]
        if line.strip() == "":
            return i  # blank line is at 1-based (i+1); last entry line is at i
        if not line[:1].isspace():
            return i  # next un-indented line begins a new directive
    return end


def format_transaction(
    date_str: str,
    flag: str,
    payee: str | None,
    narration: str,
    postings: list[tuple[str, str | None]] | list[tuple[str, str | None, dict[str, str]]],
    metadata: dict[str, str] | None = None,
    narration_max_length: int | None = None,
) -> str:
    """Format a beancount transaction as a string.

    `postings` accepts either the legacy 2-tuple `(account, amount)` shape
    or a 3-tuple `(account, amount, metadata)` where `metadata` is a per-
    posting key/value dict rendered indented under the posting line. This
    is the mechanism by which `actual:`/`settle:`/`paypal:` get attached
    to the correct leg per plugin convention. `narration_max_length`, when
    set, truncates the narration to that many characters (no ellipsis) to
    keep ledger lines readable.
    """
    payee_part = f'"{payee}" ' if payee else ""
    if narration_max_length is not None and len(narration) > narration_max_length:
        narration = narration[:narration_max_length]
    header = f'{date_str} {flag} {payee_part}"{narration}"'
    lines = [header]
    if metadata:
        for k, v in metadata.items():
            lines.append(f'  {k}: "{v}"')
    for posting in postings:
        account, amount, *rest = posting
        posting_meta: dict[str, str] = rest[0] if rest else {}
        if amount:
            lines.append(f"  {account:<40} {amount}")
        else:
            lines.append(f"  {account}")
        for mk, mv in posting_meta.items():
            lines.append(f'    {mk}: "{mv}"')
    return "\n".join(lines) + "\n"
