"""Interactive categorizer screens — the prompt layer specified in
`docs/ux-design.md`.

Each screen is a function `run(console, ctx) -> Decision`. Screens render
themselves via `render(console, ctx)` (pure I/O against `Console.print`)
and loop on `Prompt.ask` until the user commits. No `getchar`, no `Live`,
no full-screen redraw — the screen body re-renders below the previous one
on every edit, preserving scrollback (see "Decision A" in the design doc).

Step 2 of the implementation order builds Screens 1 and 3 directly. Step 3
extracts the shared `screen.py` (header + hotkey-row + Prompt.ask wrapper)
once both prototypes prove what the abstraction needs.
"""
