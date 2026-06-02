# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

This is a public repository rewrite of a vibe-coded beancount importer script. Do not leak financials.
The reference implementation is in ~/finances/import_transactions.py

Keep discussions technical, architecturally relevant and concise. don't start waffling about non-technical details.

## Commands

```sh
uv sync                                          # install deps
uv run pytest                                    # full test suite
uv run pytest tests/test_pipeline.py -k year_filter  # single test / feature
uv run ruff check src tests                      # lint
uv run pyrefly check src tests                   # type-check
uv run bean-import --help                        # CLI entry point
```

## Architecture

The codebase is a modular beancount v3 CSV importer. The key architectural principle is a **pure pipeline**: `pipeline.run()` never writes files, never touches stdin/stdout, and is deterministic given its inputs. All I/O and interaction is delegated to `cli.py`.
Keep the architecture as simple and straightforward as possible. Someone else should be able to quickly reason locally without looking at related files.

### Layer map

```
cli.py          ← typer + rich; interactive prompts; drives pipeline; calls writer
pipeline.py     ← pure run(session, categorize_fn, reporter, decisions) → list[ImportResult]
session.py      ← ImportSession (frozen Pydantic); ImportOptions
models.py       ← SourceTransaction, LedgerEntry, CategoryProposal, ImportResult (all frozen Pydantic)
config.py       ← Config.load(path) → Config; BankConfig; CsvConfig
parsers/        ← AbstractParser ABC; GenericCsvParser (config-driven); custom parsers for PayPal/cash
rules/          ← CategorizationRule; find_matching_rule; apply_rule; tags.py (active tag state)
matching/       ← normalize_text; scorer; dedup; transfers; paypal cross-reference
beancount_io/   ← reader (beancount v3 AST → LedgerEntry); writer (append + splice_all)
transforms/     ← TransformHook protocol; settle/actual/amortize metadata generation
replay.py       ← DecisionLog: record + replay one-off decisions (decisions.jsonl)
scaffolding.py  ← year-dir setup, balance assertions, document links
```

### Key invariants

- **`Decimal` everywhere** — never `float` for amounts.
- **Frozen Pydantic models** for all data types except `ProposedChange` (a `NamedTuple`).
- **`splice_all` must apply back-to-front** — splicing `.bean` files front-to-back corrupts subsequent line numbers. The writer in `beancount_io/writer.py` sorts splices by `line_start` descending.
- **Decisions are written before ledger writes** — `decisions.jsonl` is appended immediately when the user makes a choice, not gated on `bean-check` success. `bean-check` failures are routine (e.g. balance assertions across accounts); losing manual history on failure would be worse than the trade-off.
- **Intra-session mutable state** — the pipeline holds two local working variables during one `run()` call: a working rules list (rules added mid-session apply immediately to subsequent transactions) and a working active tag. Both are local to the pipeline call, not stored in `ImportSession`.

### Pipeline flow

1. `Config.load()` → parsers parse CSVs → `beancount_io/reader` loads existing ledger entries
2. `DecisionLog.lookup()` checked first (replay wins over rules)
3. Dedup filters already-imported transactions
4. New transactions → `categorize_fn` (rules engine or interactive prompt)
5. Matched transactions → scorer computes `ProposedChange` list
6. `TransformHook`s applied to proposals
7. Results returned; CLI writes files and records decisions

### Parser selection

Banks with `[banks.csv]` in config → `GenericCsvParser` (no Python needed). Banks with `parser_class` → that class instantiated with `BankConfig`. The registry is built in `parsers/__init__.py:build_parser_registry()`.

### `{year}` template resolution

`output_file` and `source_files` paths may contain `{year}`. This is substituted at pipeline time using `session.year`, not at config load — `Config` stays year-agnostic.

## Test structure

- **Unit tests** (`test_parsers.py`, `test_rules.py`, `test_matching.py`): pure functions, no filesystem
- **Integration tests** (`test_beancount_io.py`, `test_pipeline.py`, `test_replay.py`): use `tmp_path`; no network; no interactive prompts
- `tests/conftest.py` provides `deterministic_categorize` — a `CategorizeFn` test double that always returns `Expenses:Unknown` with the txn's own payee/description carried through. Reach for a custom stub only when a test specifically needs to assert on the context.
- Property-based tests via `hypothesis` in `test_parsers.py` for locale parsing functions

## Pre-commit hook

`.pre-commit-config.yaml` runs ruff, pyrefly (src only), and the full pytest suite (with a 100% coverage gate) on every commit. The hook blocks red commits — there is no "commit failing tests now, fix later" workflow. Land tests and the corresponding production change in the same commit. Activate per clone with `uv run pre-commit install`.

## Commits

periodically commit. NEVER add a 'Co-Authored-By' trailer, it is pure noise for anyone wanting to debug this later.
