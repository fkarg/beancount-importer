"""Screen 1 `[r]` — the inline rule editor.

Opened when the user saves a categorize proposal as a rule. Instead of a
silent boolean toggle, it shows exactly what becomes the rule — a MATCH → WRITE
panel — and lets the user edit every field before committing. The two halves
map one-to-one onto `CategorizationRule`:

- MATCH: which field (payee/description), how (contains/exact/regex), the
  pattern, plus bank and amount-sign filters — "when does this fire?".
- WRITE: the target account, optional payee/narration rewrites, and a tag —
  "what does it do?".

Defaults come from the current transaction + proposal, so the common case
("match this payee, book it here") needs a single `[enter]`. Scope is
create-from-proposal only; editing an already-matched rule is separate.

Hotkeys: the MATCH fields are numbered (no natural letters), but the WRITE
fields reuse Screen 1's letters — `[c]` account, `[p]` payee, `[n]` narration,
`[t]` tag — so the muscle memory carries over. `[enter]` saves, `[.]` cancels.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError
from rich.console import Console
from rich.prompt import Prompt

from beancount_importer.categorizer.screen import ask_hotkey, bottom_rule, hotkey
from beancount_importer.models import CategoryProposal, SourceTransaction
from beancount_importer.rules.models import CategorizationRule

_FIELDS: tuple[str, ...] = ("payee", "description", "either")
_MODES: tuple[str, ...] = ("contains", "exact", "regex")
_SIGNS: tuple[str, ...] = ("", "debit", "credit")
# MATCH fields keep their numbers; WRITE fields use Screen 1's letters. `""`
# is Enter (save); `"."` cancels — matching the tag menu's control keys.
_HOTKEYS: tuple[str, ...] = ("1", "2", "3", "4", "5", "c", "p", "n", "t", "", ".")


@dataclass
class _Draft:
    """Mutable working copy of the rule while the user edits it."""

    match_field: str          # "payee" | "description"
    match_mode: str           # "contains" | "exact" | "regex"
    pattern: str
    bank_key: str
    amount_sign: str          # "" | "debit" | "credit"
    target_account: str
    override_payee: str       # "" = keep original
    override_narration: str   # "" = keep original
    tag: str                  # "" = no tag


def _default_draft(txn: SourceTransaction, proposal: CategoryProposal) -> _Draft:
    field = "payee" if txn.payee else "description"
    pattern = (txn.payee or txn.description or "").strip()
    return _Draft(
        match_field=field,
        match_mode="contains",
        pattern=pattern,
        # Default to any bank — most rules are payee-based and apply across
        # banks; the user narrows to a specific bank via [4] on the rare rule
        # that needs it.
        bank_key="",
        amount_sign="",
        target_account=proposal.target_account,
        override_payee=proposal.payee or "",
        override_narration=proposal.narration or "",
        tag=proposal.tag or "",
    )


def _draft_from_rule(rule: CategorizationRule) -> _Draft:
    """Pre-fill the editor from an existing rule (the `[r]`-edit-matched path)."""
    if rule.payee_pattern and rule.description_pattern:
        field, pattern = "either", rule.payee_pattern
    elif rule.description_pattern:
        field, pattern = "description", rule.description_pattern
    else:
        field, pattern = "payee", rule.payee_pattern
    return _Draft(
        match_field=field,
        match_mode=rule.match_mode,
        pattern=pattern,
        bank_key=rule.bank_key,
        amount_sign=rule.amount_sign,
        target_account=rule.target_account,
        override_payee=rule.override_payee or "",
        override_narration=rule.override_narration or "",
        tag=rule.tag or "",
    )


def _sign_label(sign: str) -> str:
    return {"debit": "− debit", "credit": "+ credit"}.get(sign, "any")


def _build(draft: _Draft) -> CategorizationRule:
    """Materialize the draft into a validated rule (raises on bad regex).

    "either" sets both patterns to the same text with `match_any` — one rule
    matching payee OR narration, instead of a duplicated pair.
    """
    either = draft.match_field == "either"
    on_payee = draft.match_field in ("payee", "either")
    on_desc = draft.match_field in ("description", "either")
    return CategorizationRule(
        target_account=draft.target_account,
        payee_pattern=draft.pattern if on_payee else "",
        description_pattern=draft.pattern if on_desc else "",
        match_mode=draft.match_mode,  # type: ignore[arg-type]
        match_any=either,
        bank_key=draft.bank_key,
        amount_sign=draft.amount_sign,  # type: ignore[arg-type]
        override_payee=draft.override_payee or None,
        override_narration=draft.override_narration or None,
        tag=draft.tag or None,
    )


def _render(console: Console, draft: _Draft) -> None:
    console.print()
    console.print(
        f"  [bold]Save as rule[/]  —  edit, then {hotkey('enter')} save"
        f" / {hotkey('.')} cancel"
    )
    console.print()
    console.print("  [dim]MATCH — when does this fire?[/]")
    console.print(f"   {hotkey('1')} field      {draft.match_field}")
    console.print(f"   {hotkey('2')} mode       {draft.match_mode}")
    console.print(f"   {hotkey('3')} pattern    \"{draft.pattern}\"")
    console.print(f"   {hotkey('4')} bank       {draft.bank_key or 'any'}")
    console.print(f"   {hotkey('5')} sign       {_sign_label(draft.amount_sign)}")
    console.print("  [dim]WRITE — what does it do?[/]")
    console.print(f"   {hotkey('c')} account    {draft.target_account or '—'}")
    console.print(
        f"   {hotkey('p')} payee      {draft.override_payee or '(keep original)'}"
    )
    console.print(
        f"   {hotkey('n')} narration  {draft.override_narration or '(keep original)'}"
    )
    console.print(f"   {hotkey('t')} tag        {('#' + draft.tag) if draft.tag else '—'}")
    console.print(f"  {hotkey('enter')} save   {hotkey('.')} cancel")
    bottom_rule(console)


def _cycle(values: tuple[str, ...], current: str) -> str:
    return values[(values.index(current) + 1) % len(values)]


def _edit(console: Console, label: str, current: str) -> str:
    return Prompt.ask(f"{label} [{current}]", default=current).strip()


def run(
    console: Console,
    txn: SourceTransaction,
    proposal: CategoryProposal,
    existing_rule: CategorizationRule | None = None,
) -> CategorizationRule | None:
    """Render → prompt → handle until `[s]` save or `[c]` cancel.

    With `existing_rule`, the panel is pre-filled from that rule (the
    `[r]`-edit-the-matched-rule path); otherwise from the txn + proposal (a
    new rule). Returns the built `CategorizationRule` on save, `None` on cancel.
    """
    draft = (
        _draft_from_rule(existing_rule)
        if existing_rule is not None
        else _default_draft(txn, proposal)
    )
    while True:
        _render(console, draft)
        key = ask_hotkey(
            _HOTKEYS, console=console, redraw=lambda: _render(console, draft)
        )
        if key == ".":
            return None
        if key == "":  # Enter saves
            try:
                return _build(draft)
            except (ValidationError, ValueError):
                console.print(
                    f"  [yellow]invalid {draft.match_mode} pattern "
                    f"{draft.pattern!r} — fix it or change mode[/]"
                )
            continue
        if key == "1":
            draft.match_field = _cycle(_FIELDS, draft.match_field)
        elif key == "2":
            draft.match_mode = _cycle(_MODES, draft.match_mode)
        elif key == "3":
            draft.pattern = _edit(console, "pattern", draft.pattern)
        elif key == "4":
            draft.bank_key = _edit(console, "bank (empty = any)", draft.bank_key)
        elif key == "5":
            draft.amount_sign = _cycle(_SIGNS, draft.amount_sign)
        elif key == "c":
            draft.target_account = _edit(console, "account", draft.target_account)
        elif key == "p":
            draft.override_payee = _edit(
                console, "payee rewrite (empty = keep)", draft.override_payee
            )
        elif key == "n":
            draft.override_narration = _edit(
                console, "narration rewrite (empty = keep)", draft.override_narration
            )
        else:  # key == "t" (ask_hotkey restricts to _HOTKEYS)
            draft.tag = _edit(console, "tag (empty = none)", draft.tag)
