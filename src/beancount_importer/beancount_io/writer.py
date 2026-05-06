from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


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


def format_transaction(
    date_str: str,
    flag: str,
    payee: str | None,
    narration: str,
    postings: list[tuple[str, str | None]],
    metadata: dict[str, str] | None = None,
) -> str:
    """Format a beancount transaction as a string."""
    payee_part = f'"{payee}" ' if payee else ""
    header = f'{date_str} {flag} {payee_part}"{narration}"'
    lines = [header]
    if metadata:
        for k, v in metadata.items():
            lines.append(f'  {k}: "{v}"')
    for account, amount in postings:
        if amount:
            lines.append(f"  {account:<40} {amount}")
        else:
            lines.append(f"  {account}")
    return "\n".join(lines) + "\n"
