"""Persistent state header rendered at the top of every screen.

```
[12/47]  spk · 2024 · tag: italy-trip (4 left)                        ✎
```

Components:
- `[12/47]` — current/total transactions in this run
- `spk · 2024` — bank · year context
- `tag: italy-trip (4 left)` — active tag, optional `(N left)` for duration mode
- right-aligned glyph — `✎` decision needed, `?` no proposal yet, `⚡` collision
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console

from beancount_importer.categorizer.screen import RULE_WIDTH


@dataclass(frozen=True)
class HeaderContext:
    progress: tuple[int, int]
    bank_key: str
    year: int
    active_tag: str | None
    tag_remaining: int | None  # only meaningful for `duration`-mode tags
    glyph: str


def render_header(console: Console, ctx: HeaderContext) -> None:
    """Print the rule-header-rule sandwich. Pure I/O."""
    rule = "─" * RULE_WIDTH
    console.print(rule)

    current, total = ctx.progress
    progress = f"[{current}/{total}]"
    parts = [progress, f"{ctx.bank_key} · {ctx.year}"]
    parts.append(_format_tag(ctx))
    body = "  ".join(parts)
    # Right-align the glyph by padding the body up to (RULE_WIDTH - 2).
    pad = max(1, RULE_WIDTH - len(_strip_markup(body)) - 1 - len(ctx.glyph))
    console.print(f" {body}{' ' * pad}{ctx.glyph}")
    console.print(rule)


def _format_tag(ctx: HeaderContext) -> str:
    if not ctx.active_tag:
        return "no tag"
    label = f"tag: [magenta]{ctx.active_tag}[/]"
    if ctx.tag_remaining is not None:
        label += f" ([dim]{ctx.tag_remaining} left[/])"
    return label


def _strip_markup(text: str) -> str:
    """Strip Rich `[...]` style tags for width calculation only.

    Naive — assumes tags don't nest weirdly. The header markup is shallow
    (one `[magenta]` and one `[dim]` at most) so that's fine here.
    """
    out: list[str] = []
    inside = False
    for ch in text:
        if ch == "[":
            inside = True
            continue
        if ch == "]" and inside:
            inside = False
            continue
        if not inside:
            out.append(ch)
    return "".join(out)
