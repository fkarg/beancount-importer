from __future__ import annotations

import re
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from beancount.parser import parser as _bc_parser

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

    content = "".join(lines)
    target.write_text(content, encoding="utf-8")

    # Validate that the splice produced *syntactically* valid beancount — the
    # splice's only job is not to corrupt the file. We deliberately do NOT run
    # a full `bean-check`: these per-year files are fragments whose account
    # `open`s and balance context live in the root ledger, so account/balance
    # validation would false-positive ("unknown account") on every posting and
    # roll back perfectly good writes. Parse-only catches real corruption
    # (bad line offsets producing malformed beancount) without that.
    syntax_errors = _syntax_errors(content)
    if syntax_errors:
        shutil.copy2(bak, target)
        bak.unlink()
        raise RuntimeError(
            "beancount parse failed after splice — rolled back:\n"
            + "\n".join(syntax_errors)
        )
    bak.unlink()


def _syntax_errors(content: str) -> list[str]:
    """Return human-readable syntax errors from parsing `content`, if any.

    Parse-only: `beancount.parser` reports malformed syntax but never
    account/balance problems (those come from the loader against the whole
    ledger), which is exactly the distinction we want for a fragment splice.
    """
    _, errors, _ = _bc_parser.parse_string(content)
    return [getattr(e, "message", str(e)) for e in errors]


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
    after the start. Before splicing, the line at `line_start` must actually
    begin with the entry's date — stale coordinates (the file changed since
    the ledger was loaded) raise ValueError instead of silently rewriting
    whatever transaction now sits there. The bank-side leg always carries the
    original amount; any additional postings come from the proposal. Metadata
    merges entry-side metadata with proposal metadata (proposal wins) and
    includes `tag` when set; header `#tags` and `^links` are preserved.
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
    # Migrate the legacy `tag:` metadata written by older versions into a real
    # #tag, so touching an old entry repairs it rather than re-emitting bad data.
    legacy_tag = metadata.pop("tag", None)
    tags = sorted(
        {
            t
            for t in (*entry.tags, legacy_tag, proposal.tag)
            if t and t.strip()
        }
    )

    text = format_transaction(
        date_str=entry.date.isoformat(),
        flag=entry.flag,
        payee=payee,
        narration=narration,
        postings=postings,
        metadata=metadata,
        tags=tags,
        links=entry.links,
        narration_max_length=narration_max_length,
    )
    if not entry.file_path:
        # An in-session synthesized entry (e.g. a cross-bank counter-leg
        # placeholder) has no on-disk location to rewrite — `Path("")` would
        # resolve to the cwd. The pipeline must redirect such matches before
        # persistence; reaching here is a contract violation, not user error.
        raise ValueError("apply_update: entry has no file_path to splice into")
    target = Path(entry.file_path)
    lines = target.read_text(encoding="utf-8").splitlines()
    header = (
        lines[entry.line_start - 1]
        if 1 <= entry.line_start <= len(lines)
        else ""
    )
    if not header.startswith(entry.date.isoformat()):
        raise ValueError(
            f"splice target mismatch at {target}:{entry.line_start}: expected "
            f"a transaction dated {entry.date.isoformat()}, found {header!r} — "
            "line numbers are stale, refusing to splice"
        )
    line_end = entry.line_end or _detect_entry_end(lines, entry.line_start)
    splice_entries([(entry.line_start, line_end, text)], target, dry_run=dry_run)


def _detect_entry_end(lines: list[str], line_start: int) -> int:
    """Find the 1-based last line of the transaction starting at `line_start`.

    Beancount's loader only records the start line. The end is whatever comes
    before the next blank line or top-level directive — postings and metadata
    are indented, so we stop at the first un-indented line after the header.
    """
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
    tags: Iterable[str] = (),
    links: Iterable[str] = (),
    narration_max_length: int | None = None,
) -> str:
    """Format a beancount transaction as a string.

    `postings` accepts either the legacy 2-tuple `(account, amount)` shape
    or a 3-tuple `(account, amount, metadata)` where `metadata` is a per-
    posting key/value dict rendered indented under the posting line. This
    is the mechanism by which `actual:`/`settle:`/`paypal:` get attached
    to the correct leg per plugin convention.

    `tags` are beancount `#hashtags` appended to the transaction header line
    (not `tag:` metadata — those are semantically different in beancount and
    invisible to `bean-query ... IN tags`). A stray leading `#` is tolerated.
    `links` are `^links` appended after the tags, same tolerance for a stray
    leading `^`. `narration_max_length`, when set, truncates the narration to
    that many characters (no ellipsis) to keep ledger lines readable.
    """
    payee_part = f'"{payee}" ' if payee else ""
    if narration_max_length is not None and len(narration) > narration_max_length:
        narration = narration[:narration_max_length]
    header = f'{date_str} {flag} {payee_part}"{narration}"'
    header += "".join(
        f" #{t.lstrip('#')}" for t in tags if t and t.strip()
    )
    header += "".join(
        f" ^{ln.lstrip('^')}" for ln in links if ln and ln.strip()
    )
    lines = [header]
    if metadata:
        for k, v in metadata.items():
            if _is_reserved_meta_key(k):
                continue
            lines.append(f"  {k}: {_render_meta_value(v)}")
    for posting in postings:
        account, amount, *rest = posting
        posting_meta: dict[str, str] = rest[0] if rest else {}
        if amount:
            lines.append(f"  {account:<40} {amount}")
        else:
            lines.append(f"  {account}")
        for mk, mv in posting_meta.items():
            if _is_reserved_meta_key(mk):
                continue
            lines.append(f"    {mk}: {_render_meta_value(mv)}")
    return "\n".join(lines) + "\n"


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _render_meta_value(value: str) -> str:
    """Render a metadata value: bare for date-shaped values, quoted otherwise.

    beancount parses `settle: 2024-01-17` as a `date` but `settle:
    "2024-01-17"` as a `str`. Date-consuming plugins (settle/actual/paypal)
    compare against real dates, so an ISO-date value is emitted bare; everything
    else stays a quoted string.
    """
    return value if _ISO_DATE.fullmatch(value) else f'"{value}"'


def _is_reserved_meta_key(key: str) -> bool:
    """True for keys beancount reserves for its loader (`__automatic__`,
    `__tolerances__`, `__residual__`, …). These carry a leading dunder, are not
    valid metadata-key source syntax, and must never be written back — doing so
    fails the post-splice reparse (`Invalid token: '__tolerances__:'`). The
    reader also filters them, but the writer is the last line of defence for a
    robust splice regardless of where an entry's metadata originated.
    """
    return key.startswith("__")
