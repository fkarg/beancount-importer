# Screen graph

Single source of truth for what each TUI screen renders, what hotkeys it
accepts, and where each outcome routes. The host (`categorizer/host.py`) is
the dispatcher; individual screen modules implement rendering and key
parsing. Read this when you're trying to figure out "what does `[c]` do
*here*?" without grepping three files.

## Entry points

The pipeline calls into the host through two slots:

- **`make_screen_categorizer(console)`** → `CategorizeFn`. Runs once per
  transaction that needs user input. The pipeline pre-computes
  `ctx.is_ambiguous` and `ctx.seed_proposal`; rows where the seed
  proposal would produce a zero-diff update never reach the host
  (silent-skipped at the pipeline level).
- **`make_screen_merge_fn(console)`** → `MergeFn`. Runs only when the
  pipeline detects an `update` action with a non-empty `proposed_changes`
  list. Maps 1:1 to `MergeDecision.action` values.

## Routing diagram (categorize_fn entry)

```
┌───────────────────────┐
│ host._fn(ctx)         │
└──┬────────────────────┘
   │
   │ ctx.is_ambiguous?
   ├─ yes ─▶ Screen 4 (ambiguous)
   │
   │ ctx.seed_proposal not None?
   ├─ yes ─▶ Screen 1 (confirm) — kind = auto_matched | top_candidate
   │
   └─ no  ─▶ Screen 2 (pick) ─▶ Screen 1 (confirm) — kind = fresh_pick
```

`is_ambiguous=True` always implies `seed_proposal` is also set (ambiguity
requires candidates), but the ambiguous branch wins because the user
gets to pick *which* entry to merge against before confirming on Screen 1.

## Screens

### Screen 1 — `confirm` (`categorizer/confirm.py`)

Renders the proposal as it'll be written, with the matched entry's diff
highlighted when present.

| Hotkey      | Action                                         | Routes to                              |
|-------------|------------------------------------------------|----------------------------------------|
| `[enter]`   | accept proposal as-is                          | returns `CategoryProposal` (categorize) |
| `[c]`       | change account                                 | Screen 2 → Screen 1 (kind=`fresh_pick`) |
| `[n]`       | edit narration (in-place text input)           | re-renders Screen 1                     |
| `[p]`       | edit payee (in-place text input)               | re-renders Screen 1                     |
| `[t]`       | tag menu (sub-prompt)                          | re-renders Screen 1 with `tag_state_delta` |
| `[m]`       | amortize sub-prompt                            | re-renders Screen 1 with metadata       |
| `[r]`       | toggle save-as-rule                            | re-renders Screen 1                     |
| `[s]`       | skip this row                                  | returns `CategoryProposal(action="skip")` |
| `[q]`       | quit the run                                   | returns `CategoryProposal(action="quit")` |

Sub-prompts (`tag_menu`, `amortize`) are modal: they take focus, return a
delta, and Screen 1 re-renders with the delta folded in. They never bypass
Screen 1's confirm step.

### Screen 2 — `pick` (`categorizer/pick.py`)

Numbered list of suggested accounts (ranked by usage); falls through to
a column grid for the full account list.

| Hotkey            | Action                          | Routes to                            |
|-------------------|---------------------------------|--------------------------------------|
| `[1..9]`          | pick suggestion N               | back to Screen 1 (kind=`fresh_pick`) |
| `[l]`             | open full column grid           | re-renders Screen 2 with grid        |
| `[w]` / `[o]`     | filter scope (within / others)  | re-renders Screen 2                  |
| `[s]`             | skip                            | bubbles up to caller (`skip` proposal) |
| `[q]`             | quit                            | bubbles up to caller (`quit` proposal) |

Screen 2 has no Enter binding — the prompt is "pick a number", not
"confirm a value". Documented exception to the "Enter accepts" rule.

### Screen 3 — `collision` (`categorizer/collision.py`)

Fires from `make_screen_merge_fn`, NOT from the categorize_fn path.
Renders the proposed-changes diff against the matched entry.

| Hotkey      | `MergeDecision.action` | Outcome                                              |
|-------------|------------------------|------------------------------------------------------|
| `[enter]`   | `update`               | apply the auto-generated update as-is                |
| `[k]`       | `keep`                 | silent-match; mirror entry, no splice                |
| `[i]`       | `import_new`           | create a fresh entry alongside the matched one       |
| `[b]`       | `block`                | install `suppress_updates` rule, skip this row       |
| `[s]`       | `skip`                 | no-op for this run (row reappears next run)          |
| `[q]`       | `quit`                 | tear down the run                                    |

The pipeline's `_apply_merge_decision` translates each outcome into an
`ImportResult` shape; see `pipeline/run.py`.

### Screen 4 — `ambiguous` (`categorizer/ambiguous.py`)

Two or more candidates within `min_delta` of the top score, where at
least one would produce a non-empty diff. (All-zero-diff ambiguous sets
silent-skip at the pipeline level — they never reach this screen.)

| Hotkey      | Action                          | Routes to                               |
|-------------|---------------------------------|-----------------------------------------|
| `[enter]`   | pick #1 (top score)             | Screen 1 (kind=`top_candidate`, picked entry) |
| `[1..N]`    | pick #N                         | Screen 1 (kind=`top_candidate`, picked entry) |
| `[i]`       | import as new                   | Screen 2 → Screen 1 (kind=`fresh_pick`) |
| `[s]`       | skip                            | bubbles up (`skip` proposal)             |
| `[q]`       | quit                            | bubbles up (`quit` proposal)             |

## Globally consistent letters

Per `docs/ux-design.md`'s "one letter, one verb" rule, the following are
fixed across every screen they appear on:

| Letter | Verb                                                       |
|--------|------------------------------------------------------------|
| `s`    | skip (bubbles up `skip` proposal / decision)               |
| `q`    | quit (bubbles up `quit` proposal / decision)               |
| `c`    | change account (Screen 1 only — opens Screen 2)            |
| `i`    | import as new (Screens 3 & 4)                              |
| `b`    | block / install suppress rule (Screen 3 only)              |
| `k`    | keep (Screen 3 only — silent match)                        |
| `n`    | narration edit (Screen 1 only)                             |
| `p`    | payee edit (Screen 1 only)                                 |
| `t`    | tag menu (Screen 1 only)                                   |
| `m`    | amortize sub-prompt (Screen 1 only)                        |
| `r`    | save-as-rule toggle (Screen 1 only)                        |
| `l`    | full column grid (Screen 2 only)                           |

All hotkeys are **lowercase**. Muscle memory must not depend on shift
state. Letters that map to two distinct verbs across screens would
invite the silent-error class we want to avoid; the table above
guarantees this doesn't happen.

## Where state lives

The host has no per-session mutable state — every call works from the
incoming `CategorizeContext` (or `MergeContext`) plus the global
`Console`. Pipeline-driven state (`working_rules`, `working_tag`,
`existing` buckets) lives in the pipeline; the host reads it via the
context and never writes back to it. Screen sub-prompts that modify
the proposal (e.g. tag menu setting `tag_state_delta`) return the
modified proposal up through `_run_confirm`, which threads it back
through Screen 1's re-render loop.

## Adding a new screen

1. Create `categorizer/<name>.py` with `Context`, `Decision`, and `run()`.
2. Add a routing branch in `host._fn` (or `_run_<screen>` helper).
3. Add the hotkey table above and the routing edge in the diagram.
4. If the screen needs pipeline state (`is_ambiguous`-style flag),
   extend `CategorizeContext` and have the pipeline populate it
   before invoking `categorize_fn`.
5. Tests under `tests/test_categorizer_<name>.py`.
