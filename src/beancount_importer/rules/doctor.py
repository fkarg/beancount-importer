"""Rule hygiene analysis: find rules an earlier rule makes unreachable.

Rules match first-wins, so a broad rule placed before a narrower one means the
narrow rule never fires — dead configuration the user probably didn't intend.
For literal (`contains`/`exact`) rules this is statically decidable; `regex`
rules can't be compared this way and are reported as "not analyzed".

Pure and read-only — the CLI wraps `analyze_rules` + `format_report`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

from beancount_importer.rules.models import CategorizationRule


class Shadow(NamedTuple):
    """`later` (index) never fires because `earlier` (index) matches first."""

    later: int
    earlier: int


@dataclass(frozen=True)
class HygieneReport:
    shadows: tuple[Shadow, ...]
    manual: tuple[int, ...]  # regex rules — not statically analyzable


def _single_field(rule: CategorizationRule) -> tuple[str, str] | None:
    """Return `(field, pattern)` when exactly one pattern is set, else None."""
    if rule.payee_pattern and not rule.description_pattern:
        return ("payee", rule.payee_pattern)
    if rule.description_pattern and not rule.payee_pattern:
        return ("description", rule.description_pattern)
    return None


# Characters that make a regex mean more than its literal text. Note space,
# `-`, `&`, `#`, `~` are deliberately excluded — `re.escape` escapes them but
# they match literally under `re.search`, so a pattern like "REWE Filiale" is
# still a plain substring match.
_REGEX_META = set(".^$*+?{}[]\\|()")


def _effective_mode(rule: CategorizationRule) -> str | None:
    """The mode to compare a single-field rule by, or None if undecidable.

    A `regex` pattern with no regex-significant metacharacters is a plain
    literal — `re.search` of it is exactly a `contains` substring match — so it
    can be analyzed like one. A genuine regex returns None: "not analyzed".
    """
    if rule.match_mode != "regex":
        return rule.match_mode
    fld = _single_field(rule)
    if fld is not None and not (set(fld[1]) & _REGEX_META):
        return "contains"
    return None


def _filters_broader_or_equal(
    earlier: CategorizationRule, later: CategorizationRule
) -> bool:
    """True when `earlier`'s bank/sign filters are no narrower than `later`'s."""
    if earlier.bank_key and earlier.bank_key != later.bank_key:
        return False
    return not (earlier.amount_sign and earlier.amount_sign != later.amount_sign)


def _shadows(earlier: CategorizationRule, later: CategorizationRule) -> bool:
    """Does `earlier` make `later` unreachable (every `later` match also hits
    `earlier`)? Conservative: only decides literal, single-field rules."""
    fe, fl = _single_field(earlier), _single_field(later)
    if fe is None or fl is None:
        return False
    me, ml = _effective_mode(earlier), _effective_mode(later)
    if me is None or ml is None:  # a genuine regex on either side
        return False
    if fe[0] != fl[0]:  # different match field
        return False
    if not _filters_broader_or_equal(earlier, later):
        return False
    pe, pl = fe[1].casefold(), fl[1].casefold()
    if me == "contains":
        # `earlier` matches any haystack containing `pe`; it shadows `later`
        # iff everything `later` matches necessarily contains `pe` — i.e. `pe`
        # is a substring of `pl` (true for both contains- and exact-mode later).
        return pe in pl
    # `earlier` is exact: it only matches the single string `pe`, so it can
    # shadow `later` only if `later` is also exact on the same string.
    return ml == "exact" and pe == pl


def analyze_rules(rules: list[CategorizationRule]) -> HygieneReport:
    shadows: list[Shadow] = []
    for j in range(len(rules)):
        for i in range(j):
            if _shadows(rules[i], rules[j]):
                shadows.append(Shadow(later=j, earlier=i))
                break  # earliest shadower is enough to prove unreachable
    manual = tuple(
        k for k, r in enumerate(rules)
        if _single_field(r) is not None and _effective_mode(r) is None
    )
    return HygieneReport(shadows=tuple(shadows), manual=manual)


def _describe(rule: CategorizationRule) -> str:
    fld = _single_field(rule)
    if fld is None:
        return f"-> {rule.target_account}"
    scope = f" bank={rule.bank_key}" if rule.bank_key else ""
    return f"{fld[0]} {rule.match_mode}: {fld[1]!r}{scope}"


def format_report(
    rules: list[CategorizationRule], report: HygieneReport
) -> list[str]:
    """Human-readable lines for the CLI. Report-only — nothing is changed."""
    lines: list[str] = []
    if report.shadows:
        lines.append(f"SHADOWED — {len(report.shadows)} rule(s) never fire:")
        for s in report.shadows:
            lines.append(f"  #{s.later} {_describe(rules[s.later])}")
            lines.append(f"     shadowed by #{s.earlier} {_describe(rules[s.earlier])}")
    else:
        lines.append("no shadowed rules found.")
    if report.manual:
        idxs = ", ".join(f"#{k}" for k in report.manual)
        lines.append(f"NOT ANALYZED — {len(report.manual)} regex rule(s): {idxs}")
    return lines
