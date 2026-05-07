"""Categorization modes — sub-prompts behind Screen 1's `[m]` hotkey.

Each mode augments a `CategoryProposal`'s metadata so a downstream
`TransformHook` (e.g. `transforms/amortize.py`) expands it on output.
Modes live in their own modules so a future `CategorizationMode`
registry can load them by dotted path — same pattern as transforms
and matchers — without changing the screen layer.
"""
