"""Amortize mode — Screen 1's `[m]` hotkey target (Phase 4 task 4.1).

Stamps the proposal's metadata with the `<type>: <months>` pair the
upstream amortize plugin reads. The transform hook in
`transforms/amortize.py` continues to handle the rule-driven path; this
module is the interactive equivalent for one-off amortizations.
"""

from __future__ import annotations

from rich.console import Console
from rich.prompt import Prompt

from beancount_importer.categorizer.screen import bottom_rule, hotkey
from beancount_importer.models import CategoryProposal


# Valid amortize_type values. Match the plugin's conventions exactly:
# - lifetime_months — depreciation over expected lifetime
# - prepaid_months  — prepaid expense, recognized monthly
# - amortize_months — generic amortization with no intermediate asset
_AMORTIZE_TYPES: tuple[tuple[str, str], ...] = (
    ("1", "amortize_months"),
    ("2", "prepaid_months"),
    ("3", "lifetime_months"),
)
_HOTKEYS: tuple[str, ...] = tuple(k for k, _ in _AMORTIZE_TYPES) + ("4",)
_DEFAULT_MONTHS = 12


def render(console: Console) -> None:
    """Render the amortize-mode menu. Pure I/O."""
    console.print()
    console.print("  [bold]Amortize[/]  [dim](spread cost over multiple months)[/]")
    console.print(f"    {hotkey('1')} amortize_months   [dim](generic)[/]")
    console.print(f"    {hotkey('2')} prepaid_months    [dim](prepaid expense)[/]")
    console.print(f"    {hotkey('3')} lifetime_months   [dim](depreciation)[/]")
    console.print(f"    {hotkey('4')} cancel            [dim](no change)[/]")
    bottom_rule(console)


def run(console: Console, proposal: CategoryProposal) -> CategoryProposal:
    """Render → prompt → return either the augmented proposal or the
    original (cancel / invalid month count).

    Invalid month input cancels with a warning rather than aborting the
    user's whole categorize decision — the design principle is "no
    surprises after Enter", and a typo here shouldn't burn a transaction.
    """
    render(console)
    # No Enter default: same reasoning as tag_menu — the screen has no
    # "obvious" pick, and a silent cancel-on-Enter is exactly the bug
    # we hit on the pager.
    key = Prompt.ask(
        ">",
        choices=list(_HOTKEYS),
        show_choices=False,
        show_default=False,
    ).strip()
    if key == "4":
        return proposal
    type_map = dict(_AMORTIZE_TYPES)
    a_type = type_map[key]
    months = _prompt_months(console)
    if months is None:
        return proposal
    return proposal.model_copy(
        update={"metadata": {**proposal.metadata, a_type: str(months)}}
    )


def _prompt_months(console: Console) -> int | None:
    """Months prompt with `_DEFAULT_MONTHS` default. Returns None on
    invalid input (with a brief warning) so the caller can keep the
    proposal unchanged.
    """
    raw = Prompt.ask(
        "months", default=str(_DEFAULT_MONTHS)
    ).strip()
    try:
        months = int(raw)
        if months < 1:
            raise ValueError
    except ValueError:
        console.print(f"  [yellow]invalid month count {raw!r} — cancelling[/]")
        return None
    return months
