# Refactor smells & proposed moves

A focused tech-debt audit triggered by the question "what do you think of the
state of this project?" — the architecture is healthy, but six smells stand
out. Each section below states the smell, the proposed move, the
**architectural tradeoff** where one exists, and any **smaller calls I'd just
make** without asking.

**Status:** read and decided. Felix's calls are inlined under each smell as
**Decision:** blocks. Execution order at the bottom.

---

## Smell 1 — `_process_transaction` is a 240-line, 11-step procedure

### Where

`src/beancount_importer/pipeline.py:389-648`

### What it looks like today

The function is a numbered procedure (literally — comments labelled `# 0.`
through `# 11.`):

0. Apply rule overrides for matching parity.
1. Replay log lookup.
2. Dedup → claim entry.
3. Hard skip patterns.
4. Cross-source matchers (skip / rewrite_target).
5. Score candidates.
6. Choose proposal source (matcher / auto-threshold / categorize_fn).
7. Apply transforms.
7b. Apply user tag-state delta.
8. Auto-stamp active tag onto proposal.
9. Synthesize a "save as rule" if requested.
10. Compute tag-state delta for persistence.
11. `_build_result`.
12. Merge prompt (Screen 3).
13. Claim matched entry.

Each step has good prose explaining *why* it sits where it sits. That's the
load-bearing part: the order is **not arbitrary** — replay must precede dedup
must precede matchers must precede scoring, and the claim must come last. The
function reads as a procedure because the underlying logic *is* a procedure.

### Why it smells anyway

- The function is the second-most-edited file in the recent history (the first
  is the categorizer). Every change touches a function with 11 implicit
  invariants, and the only way to know which step you're allowed to move is to
  re-derive the constraints from the comments.
- The pipeline's two mutable locals (`working_tag`, `working_rules`) thread
  through all 11 steps as positional arguments. That widens the blast radius
  of any new state.
- Five of the steps already extract to private helpers
  (`_apply_rule_overrides`, `_compute_near_misses`, `_apply_merge_decision`,
  `_build_result`, `_claim_matched_entry`). The remaining inline steps don't
  earn their inlining — they're *also* discrete units, just not extracted.

### Proposed move

Decompose into a sequence of **named, single-responsibility steps** that each
take and return a small `TxnState` (or pass tuples), preserving the procedural
shape. **Do not** try to make this functional / monadic / pipeline-of-stages
— the order constraints are real, and a clever composition would hide them.

Concretely, three groups of helpers:

```python
# Short-circuit phase — each returns ImportResult | None.
def _try_replay(...) -> ImportResult | None: ...
def _try_dedup(...) -> ImportResult | None: ...
def _try_skip_pattern(...) -> ImportResult | None: ...
def _try_matcher_skip(...) -> ImportResult | None: ...

# Proposal phase — returns CategoryProposal.
def _resolve_proposal(...) -> CategoryProposal:
    """Matcher rewrite, auto-threshold, or categorize_fn."""

# Finalise phase — returns ImportResult.
def _finalise(...) -> ImportResult:
    """Apply transforms, tag, save_as_rule, build result, merge prompt, claim."""
```

`_process_transaction` then becomes ~30 lines: the four short-circuit checks,
proposal resolution, finalise. The numbered comments stay (they document the
*sequence*) but the bodies move.

### Tradeoff to flag

**Functional decomposition vs. procedural honesty.** The current code is one
long function partly because the steps share state (`working_tag`,
`working_rules`, the existing-entry bucket). Splitting into helpers means
either:

- (a) **Pass state explicitly through tuples / a `TxnState` dataclass.**
  Verbose call sites; no hidden state. Easy to test each helper in isolation.
- (b) **Use a small `_TxnRun` class** that owns the mutable state for one
  txn and exposes the helpers as methods. Less boilerplate; introduces an
  object whose only job is to die after one call.

I'd pick **(a)** — the verbosity is the point. A frozen `TxnState` Pydantic
model (or NamedTuple) per phase makes it obvious which fields each step reads
vs. produces, and matches the "frozen everywhere" convention. But it adds ~60
lines of plumbing, so this is the call to push back on if you'd rather keep
the function long.

> **Decision:** Go with `TxnState`, **functionally composable** — every step
> takes a state and returns a *new* state (or `ImportResult` for terminals),
> never mutates in place. Mutation-free composition makes each step
> independently testable and keeps the "frozen everywhere" invariant honest
> across phases.

### Smaller calls I'd make myself

- Drop the `# 0.` / `# 1.` numbering once the steps are named functions —
  function names + module order communicate sequence well enough.
- Inline `_proposal_from_outcome` and `_proposal_from_rule` into
  `_resolve_proposal`; they're each used exactly once.
- `_advance_tag` returns `None` from three of its four branches and is called
  three times in the function — collapse the call sites once the function is
  decomposed.

---

## Smell 2 — Categorizer host imports a private symbol from pipeline

### Where

`src/beancount_importer/categorizer/host.py:46-51`

```python
from beancount_importer.pipeline import (
    CategorizeContext,
    MergeContext,
    MergeDecision,
    _diff_changes,        # ← leading underscore
)
```

`_diff_changes` is also called from `host.py:92` to decide whether the
silent-skip path is safe (every ambiguous candidate produces a zero diff).

`tests/test_pipeline_merge_path.py` reaches for `_block_update_rule` the same
way.

### Why it smells

The leading underscore is Python's only "internal API" signal. Importing it
from a sibling module says either:

1. The function shouldn't be private (in which case rename), or
2. The host shouldn't be doing the silent-skip check (in which case move).

Right now we're getting the worst of both: the function is named as if it's
internal but is part of the host's contract.

### Proposed move

**Promote `_diff_changes` and `_block_update_rule` to public names** —
`diff_proposal_against_entry` and `build_block_update_rule` — and re-export
from `pipeline/__init__.py` (or wherever the pipeline package lands; see Smell
3). Keep `_apply_rule_overrides` private; that one really is internal.

Optionally move `diff_proposal_against_entry` to a new `matching/diff.py`
module — it's a pure function over `(LedgerEntry, CategoryProposal,
CategorizationRule)` and doesn't need to live in `pipeline.py` at all.

### Tradeoff to flag

**Where does silent-skip detection belong?** Today the host computes "would
this proposal actually change anything?" before invoking the screens. That's
arguably a pipeline concern — the pipeline already builds `proposed_changes`
in `_build_result`. The host is duplicating the check earlier so the user
doesn't see a screen for a no-op.

Two ways to dissolve the duplication:

- (a) **Pipeline computes silent-skip and short-circuits before
  `categorize_fn`.** Move the check into `_resolve_proposal`. The
  `categorize_fn` only ever sees txns where the user genuinely has a choice.
  Cleaner; but it means the host can't influence what counts as
  "non-trivial" (and Screen 4's ambiguous-set silent-skip needs the
  `min_delta` config which currently lives only in the host).
- (b) **Keep the check in the host, expose the helper publicly.** Smaller
  change. The host owns "should I render a screen?", which is a UI concern.

I'd pick **(b)** — the host is the right owner because the silent-skip
threshold (`min_delta`) is a UI parameter, and the screens are the only
caller that needs the early-exit. But this is the most-defensible-different-
way I can imagine, so flag it if you'd rather have the pipeline decide.

> **Decision:** Go with **(a)** — pipeline owns silent-skip. *But* the rule
> is narrower than "skip anything an update wouldn't change":
>
> - **Zero-diff updates** (proposal produces no `proposed_changes` against
>   the matched entry): pipeline silent-skips, no UI involvement. The user
>   has nothing to consent to.
> - **Non-zero-diff updates**: must still surface to the user. Even if the
>   pipeline could "auto-merge", the principle is *Enter is minimal consent*
>   — no ledger writes happen without the user pressing at least one key,
>   except in explicit auto-apply modes (e.g. `--auto-threshold`).
>
> Concretely: pipeline gains a "would this update be a no-op?" check
> after `_build_result`, before invoking `merge_fn`. If `proposed_changes`
> is empty AND it's an `update`, return a silent-skip result and bypass
> `merge_fn` entirely. The host's pre-emptive `_diff_changes` calls go
> away. `min_delta` (the ambiguous-set silent-skip case) moves into
> `MatchingConfig` so the pipeline can read it.
>
> Implication: `_diff_changes` stops being a host-imported helper. It can
> stay private to the pipeline package.

### Smaller calls I'd make myself

- Rename the host helper `_is_ambiguous` → `_top_two_within_delta`; the
  current name is too generic for a 3-line predicate.
- `_tag_remaining` is duplicated in `host.py` (`_tag_remaining`,
  `_merge_tag_remaining`). Same body, two contexts. Lift to a single
  `_remaining_days(active_tag, booking_date)` helper that takes the tag and
  date directly.

---

## Smell 3 — `pipeline.py` is two unrelated jobs in one file

### Where

`src/beancount_importer/pipeline.py` — 1305 lines, contains:

- `run(...)` and the per-txn machinery (≈900 lines)
- `compute_bean_provenance_stats(...)` and friends (≈400 lines)

These share the file but not much else. The provenance stats run *backwards*
— for each existing ledger entry, does any CSV row match? — and is only used
by the `--preview` command. They share `_load_all_outputs`, `_parse_all_inputs`
and `_select_banks` with `run`, but no domain types beyond those.

### Why it smells

- The single longest file in the project, dominating the architecture diagram
  in `CLAUDE.md`. Splitting it means the diagram matches the layout.
- The provenance code calls `bean-query` via `subprocess` — that's a *very*
  different I/O profile from `run`'s "no I/O beyond CSV reads + decisions
  append". Newcomers reading `CLAUDE.md`'s "pure pipeline" claim and then
  finding `subprocess.run` in the same file will be (correctly) confused.
- The `--preview` path uses a `reporter=None` to silence parse errors; that
  signal-by-None convention exists *only* because the two functions share
  `_parse_all_inputs`. Splitting removes the kludge.

### Proposed move

```
pipeline/
├── __init__.py     # re-exports run, CategorizeContext, etc. (back-compat)
├── run.py          # the live import path
├── preview.py      # compute_bean_provenance_stats + helpers
└── _shared.py      # _load_all_outputs, _parse_all_inputs, _select_banks
```

The `__init__.py` re-exports keep every existing import path working
(important — there are 14 call sites across src + tests).

### Tradeoff to flag

**Module vs. package.** Going from `pipeline.py` → `pipeline/` is a small but
real change in the codebase's surface — it moves the file Git tracks and
breaks blame. The alternative is to keep the module and just have `preview.py`
as a sibling at the package root. I'd go with the package layout because
"pipeline is a *concept* with multiple files" reads better than "pipeline is
a file and preview is some other file at the same level", but it's a
judgement call.

> **Decision:** Package layout. Don't worry about blame/history.

### Smaller calls I'd make myself

- Drop the `reporter=None` parse-error-swallowing convention once `preview.py`
  has its own preview-friendly reporter (a `NoopReporter` subclass that
  collects errors into a list for summary display).
- The `_amount_cents`, `_has_csv_match`, and `_expanded_counts` helpers are
  all preview-only — they move with `compute_bean_provenance_stats` and don't
  need re-exports.

---

## Smell 4 — Rules layer is anemic relative to where it's heading

### Where

- `src/beancount_importer/rules/engine.py` — 15 lines (one function: linear
  scan, first match wins).
- `src/beancount_importer/rules/models.py` — 73 lines, `CategorizationRule`
  is a flat record with regex fields + suppression flags + transform inputs.
- `src/beancount_importer/rules/storage.py` — 23 lines, JSON dump/load.

### Why it smells

`CategorizationRule` is doing four jobs:

1. **Match predicate** — `payee_pattern`, `description_pattern`,
   `amount_sign`, `bank_key` (two regex fields + sign + bank).
2. **Transform output** — `override_payee`, `override_narration`, `tag`.
3. **Update suppression** — four `suppress_*` flags.
4. **Transform inputs** — `settle_days`, `add_actual_date`, `amortize_months`,
   `amortize_type`.

The README's WIP section already names "match the reference implementation's
matching invariances" as the next big body of work. When you add **amount
bands** ("anything €20–€50 from Aldi"), **date windows** ("only between Jan
and Mar 2024"), or **multi-pattern AND/OR**, this model rebels:

- More flat fields (the current trajectory) — you end up with
  `amount_min`, `amount_max`, `amount_currency`, `date_after`,
  `date_before`, `description_pattern_any`, `description_pattern_all`, …
- The `matches()` method becomes 60 lines of `if self.X: ...` clauses, each
  of which has to gracefully no-op when the field is unset. Every new
  predicate pays an N+1 cost.

### Proposed move

Split the rule into **predicate** and **action** parts, with the predicate
being a small algebra:

```python
class Predicate(BaseModel, frozen=True):
    """One match condition. Subclasses: PayeeRegex, DescriptionRegex,
    AmountSign, AmountBand, DateWindow, BankIs."""
    def matches(self, txn: SourceTransaction) -> bool: ...

class CategorizationRule(BaseModel, frozen=True):
    target_account: str
    predicates: tuple[Predicate, ...] = ()  # all must match (AND)
    # ... action fields stay as today (overrides, suppression, transforms)
```

Storage stays JSON; the predicates serialise via Pydantic's discriminated
unions on a `kind: Literal[...]` field. Migration from the existing flat
schema is a one-time `_legacy_to_predicates(...)` reader path that splits
`payee_pattern` / `description_pattern` / `amount_sign` / `bank_key` into the
appropriate `Predicate`s.

This puts the rule layer where it'll need to be when you do the
reference-implementation matching audit. New predicate types become new
classes with one method, not five new fields each.

### Tradeoff to flag

**Discriminated-union predicates vs. just adding more flat fields.** The
current flat shape has real virtues:

- (a) **Flat:** trivially serialised, easy to inspect in the JSON, every
  field has a default so you can write a one-key rule. Cost: every new
  predicate touches the `matches()` body.
- (b) **Predicate algebra:** extensible (new predicate = new file in
  `rules/predicates/`), composable (AND is just "all match", OR could land
  later), but JSON gets nested and writing rules by hand is harder.

The audit-the-reference-implementation work the README mentions is the
**forcing function**. If that audit will only ever produce 1-2 new predicate
kinds (e.g. amount band), stick with flat. If it'll produce a dozen
(reference implementation has its own quirks per bank, currency-specific
rules, etc.), pay the predicate-algebra cost now.

I genuinely don't know which it'll be — that's the question to bring back
when you do the matching audit. **Don't refactor this preemptively.**

> **Decision:** Defer until the matching audit. Two things to keep in mind
> when we get there:
>
> - **In-bean-import migration paths are fair game to break.** No
>   compatibility burden between today's `CategorizationRule` shape and
>   whatever predicate algebra lands later. We're still building.
> - **A full migration path *from* the legacy/reference implementation is
>   required.** Whatever the rules layer ends up looking like, it must be
>   able to ingest the reference's rule shape. That's the import direction
>   that matters; the export direction (bean-import → reference) is not a
>   goal.

### Smaller calls I'd make myself

- The rules engine's `find_matching_rule` returns `None` on no-match. When we
  add predicate composition, that signature stays. No change.
- Once `CategorizationRule` grows past ~10 fields total, split the storage
  format version. Add a `version: int = 1` field today, even before any other
  change — it's free insurance.

---

## Smell 5 — `categorizer/` has implicit screen-routing logic in `host.py`

### Where

`src/beancount_importer/categorizer/host.py` — 427 lines coordinating six
screen modules (`confirm`, `pick`, `ambiguous`, `collision`, `tag_menu`,
`amortize`).

### Why it smells

The host's routing is a state machine, but it's expressed as nested
conditionals and `while True` loops:

- `_fn` decides Path A (rule/candidate) vs. Path B (no candidate).
- Path A has three sub-paths (silent-skip, ambiguous, confirm).
- `_run_confirm` loops on `change_account`: Screen 1 → Screen 2 → Screen 1
  with mutated state.
- `_run_ambiguous` returns four ways: pick → Screen 1, import_new → Path B,
  skip, quit.

The state machine is *correct* — there's a comment-doc at the top of `host.py`
explaining the routing, and the `[c]` round-trip in `_run_confirm` is
explained inline. But:

- Adding Screen 5/6 means weaving more branches into the same nest.
- Test coverage for routing (vs. individual screens) lives in
  `test_categorizer_host.py` (670 lines). That's a lot of test for a
  responsibility that should be a 50-line state diagram.
- The "what state are we in?" question has no single source of truth —
  routing decisions are split between `_fn`, `_run_ambiguous`,
  `_run_confirm`, and `_run_pick_then_confirm`.

### Proposed move

**Don't** introduce a state-machine framework. The screens are correct and
the routing is small enough to stay inline. But:

1. **Lift the routing to a single `_route(ctx)` function** that returns an
   enum of `RouteAction` values: `silent_skip`, `confirm(seed, kind, entry)`,
   `ambiguous`, `pick_then_confirm`. The current `_fn` body is split between
   "decide route" and "execute route"; separating them makes the state
   machine readable as a flat `match` statement.

2. **Document the screen graph in one place.** A `docs/screen-graph.md`
   file that just lists the edges:

   ```
   Screen 1 confirm:
     [enter]  → categorize result
     [c]      → Screen 2 (then back to Screen 1, kind=fresh_pick)
     [s]      → skip result
     [q]      → quit result
   Screen 2 pick:
     selection → Screen 1 (kind=fresh_pick)
     [s] / [q] → bubble up unchanged
   …
   ```

   This already exists in scattered docstrings; collecting it removes the
   "trace through three files to know what `[c]` does" tax.

That's it. No framework, no state-machine library — just a routing function
and a doc.

### Tradeoff to flag

None major. The temptation is to introduce a `ScreenStateMachine` class with
edges and transitions; resist. Six screens with ≤4 transitions each doesn't
earn a framework. Inline routing with a clean `_route()` function and
external documentation is the right scale.

> **Decision:** Agreed on the proposed move. Also flagged: TUI testability
> needs more thought generally — same goes for the `beancount_io/writer.py`
> path (currently excluded from the coverage gate). Worth a separate
> conversation; not blocking this refactor.

### Smaller calls I'd make myself

- `_run_confirm` and `_run_ambiguous` and `_run_pick_then_confirm` all return
  `CategoryProposal`. The `_confirm_to_proposal` translator is fine. But the
  three functions duplicate the `if action == "skip" return ...; if action
  == "quit" return ...` boilerplate. Lift to a single
  `_decision_to_proposal(decision)` and call from each.
- Drop the `del txn` in `_proposal_from_outcome` — the unused arg signals "I
  *might* use this later", which is exactly the half-finished implementation
  the project conventions discourage. Either use it or remove it.

---

## Smell 6 — Stale design docs and partially-stale README

### Where

- `docs/ux-design.md` and `docs/ux-design-v1.md` — both untracked
  (`git status` shows them as `??`), with no clear indication which
  supersedes which.
- `README.md`'s WIP section still lists "adapt cli to reference
  implementation" as in-progress, but the screen-driven categorizer landed
  weeks ago.

### Decision

- **Leave `ux-design.md` / `ux-design-v1.md` untracked.** They're
  ephemeral working docs; Felix will delete them later. Don't stage either.
- **README pass only:** the "WIP" section's "adapt cli to reference
  implementation" sub-bullets are largely done — clean up. The "Todo"
  section's "fuzzy picker" item stays. Anything still genuinely WIP stays.

---

## Bonus — `decisions.jsonl` schema observations

Felix flagged that the tool isn't in production yet, so format changes are
fair game. Five things noticed while reading `replay.py`:

1. **No schema version field.** Each line is a flat dict with `ts`,
   `session`, `bank`, `sig`, `decision`. If the proposal serialisation
   changes, old logs become opaque. **Add `version: 1` now** — costs nothing,
   buys forward-compat insurance.
2. **`session` field is written but never read on load.** Either drop it or
   use it. A genuine use case: `bean-import decisions undo --session <id>`
   to roll back the last session's decisions during development. Worth
   keeping iff that command lands; otherwise drop.
3. **`bank` field is written but never read on load.** The signature key is
   `sepa:` or `hash:` only — no bank scope. If two banks ever produced the
   same SEPA ref or content hash, lookup would silently collide. Either
   drop the field, or include it in the lookup key (`sepa:N26:DE89…`).
   Recommend the latter — defence in depth, costs almost nothing.
4. **Records are not human-grep-friendly.** A line like
   `{"ts":"…","sig":{"hash":"a3f…"},"decision":{"action":"categorize",…}}`
   tells you nothing about the underlying transaction without cross-
   referencing the source CSV. Adding `payee`, `narration`, `amount`,
   `date` as observability fields (alongside the hash) would make the log
   inspectable with `jq` / `grep`. Hash stays the lookup key; readable
   fields are pure diagnostic.
5. **Corrupt lines silently dropped.** `_load` swallows `JSONDecodeError`
   without surfacing it. Defensible (one bad line shouldn't kill the
   import) but should at minimum log a warning. Wire through the reporter
   when one's available, or print to stderr.

None of these are urgent. (1) and (3) are essentially free to add and worth
doing the next time the schema's touched. (4) is the highest-value if you
ever debug a "why did this row replay weirdly?" issue. Hold off on (2)
until the use case is concrete.

---

## Suggested execution order

If you decide to act on this:

1. **Smell 6** (docs hygiene) — 10 minutes, no risk, do first.
2. **Smell 2** (rename `_diff_changes`, `_block_update_rule` → public,
   move silent-skip helper) — small, mechanical, unblocks Smell 1.
3. **Smell 3** (split `pipeline.py` into a package) — mechanical, all
   imports re-export, no behaviour change.
4. **Smell 1** (decompose `_process_transaction`) — the highest-value but
   also the most invasive change. Do it after Smells 2 & 3 so the helpers
   land in the right files.
5. **Smell 5** (extract routing function + screen-graph doc) — independent
   of 1-4, can happen any time.
6. **Smell 4** (rules predicate algebra) — defer until the
   reference-implementation matching audit. Bring back the question then.

The first three are the lowest-risk and unlock the others. Smell 4 is the
one I'd genuinely **not** do without more information.

---

## What this plan does *not* propose

A few things I considered and rejected:

- **Test split / restructure.** Tests are 12k lines, and a few files are
  large (`test_pipeline.py` at 2472, `test_matching.py` at 712), but they're
  organised by behaviour not by source file (good), and the 100% coverage
  gate means churn here would risk coverage dips for no architectural
  benefit. Don't touch.
- **Pydantic frozen → dataclass migration anywhere.** The frozen-Pydantic
  convention is consistent and well-defended in `CLAUDE.md`. No reason to
  break it.
- **`splice_all` / writer changes.** The back-to-front splicing convention
  is a load-bearing invariant and the writer is excluded from coverage by
  design. Leave it alone.
- **Decision log format changes.** `decisions.jsonl` is on-disk persistence
  with replay semantics; changing the schema is a migration concern, not a
  refactor.
