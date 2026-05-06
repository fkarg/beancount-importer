"""TransformHook protocol + registry loader.

Hooks are pure functions (or instances): they take a CategoryProposal-so-far
and a (txn, rule) context, and return a new CategoryProposal. They compose
left-to-right in the order configured by `Config.transforms.enabled`.

Adding a new transform = drop a module in this package that exposes a top-level
`hook: TransformHook` instance, then list its dotted import path in config.
"""

from __future__ import annotations

import importlib
from typing import Protocol, runtime_checkable

from beancount_importer.models import CategoryProposal, SourceTransaction
from beancount_importer.rules.models import CategorizationRule


@runtime_checkable
class TransformHook(Protocol):
    name: str

    def applies_to(self, rule: CategorizationRule) -> bool: ...

    def apply(
        self,
        proposal: CategoryProposal,
        txn: SourceTransaction,
        rule: CategorizationRule,
    ) -> CategoryProposal: ...


def load_transforms(module_paths: list[str]) -> list[TransformHook]:
    """Import each module path and collect its top-level `hook` attribute.

    Validates that the loaded object satisfies TransformHook structurally; raises
    TypeError if not, so misconfiguration fails fast at session start rather than
    silently producing wrong metadata.
    """
    hooks: list[TransformHook] = []
    for path in module_paths:
        module = importlib.import_module(path)
        hook = getattr(module, "hook", None)
        if hook is None:
            raise TypeError(f"Transform module {path!r} has no top-level `hook`")
        if not isinstance(hook, TransformHook):
            raise TypeError(f"Transform {path!r}.hook does not satisfy TransformHook")
        hooks.append(hook)
    return hooks


def apply_transforms(
    hooks: list[TransformHook],
    proposal: CategoryProposal,
    txn: SourceTransaction,
    rule: CategorizationRule,
) -> CategoryProposal:
    """Run all `hooks` whose `applies_to(rule)` returns True, left-to-right."""
    for hook in hooks:
        if hook.applies_to(rule):
            proposal = hook.apply(proposal, txn, rule)
    return proposal
