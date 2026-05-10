# beancount-importer

A modular CSV importer for [beancount](https://beancount.github.io/) v3 ledgers.
Reads bank exports, dedupes against your existing ledger, applies categorization
rules, and writes new transactions — interactively or unattended.

## Install

The project uses [uv](https://docs.astral.sh/uv/). For day-to-day use, install
the CLI as a global tool:

```sh
uv tool install --editable /path/to/beancount-importer
```

`--editable` links the tool to your checkout, so `git pull` updates the binary
on your `PATH` without reinstalling. After install:

```sh
bean-import --help
```

If you previously installed a non-editable version (`uv tool install beancount-importer`
without `--editable`), reinstall it once with `--reinstall --editable` so future
source changes flow through. To verify which copy is on your `PATH`:

```sh
head -1 $(which bean-import)   # should point inside ~/.local/share/uv/tools/
```

For development against the checkout itself, `uv sync` + `uv run bean-import`
also works.

## First-time setup

Pick a directory that will hold your ledger, rules, and CSV exports:

```sh
mkdir -p ~/finances && cd ~/finances
bean-import --init
```

This produces:

```
~/finances/
├── .beancount-importer/
│   └── config.toml          ← edit this
├── documents/               ← drop CSV exports here
└── transactions/            ← per-year .bean files written here
```

All importer config + state (`config.toml`, `rules.json`, `decisions.jsonl`,
`tag_state.json`) lives in `.beancount-importer/`, keeping the project root
clean. Paths in `config.toml` are resolved relative to that folder, so
siblings like `documents/` and `transactions/` are referenced via `../`.

Edit `.beancount-importer/config.toml` and uncomment the `[[banks]]` section
for each bank you import from. The starter has a worked example for Sparkasse.

A typical `[[banks]]` entry:

```toml
[[banks]]
key = "spk"
display_name = "Sparkasse"
account = "Assets:B:SPK"
file_glob = "../documents/**/SPK_*.csv"   # recurse through year subdirs
output_file = "../transactions/{year}/SPK.bean"

[banks.csv]
delimiter = ";"
date_format = ["%d.%m.%y", "%d.%m.%Y"]
amount_locale = "de"               # "de" → 1.234,56 / "en" → 1,234.56
field_date = "Buchungstag"
field_amount = "Betrag"
field_currency = "Waehrung"
field_payee = "Beguenstigter"
field_description = "Verwendungszweck"
field_sepa_reference = "Kundenreferenz"
```

## Running an import

```sh
uv run bean-import                    # all years, all banks
uv run bean-import 2024               # just 2024
uv run bean-import 2022 2023 2024     # multiple years
```

This will:
1. Parse all CSVs matching each bank's `file_glob`.
2. Dedupe against existing entries in `output_file` and any extra `source_files`.
3. Apply categorization rules from `categorization_rules.json`.
4. Prompt you for anything not covered by a rule (target account, save-as-rule).
5. Append each new entry to `transactions/<its-booking-year>/<BANK>.bean` —
   a single import that spans multiple years splits cleanly into the
   per-year files you already have on disk.

### Useful flags

| Flag | What it does |
|---|---|
| `--config`, `-c` | Path to the importer config TOML (default: `./.beancount-importer/config.toml`) |
| `--bank`, `-b spk` | Only process one bank by key |
| `--year-filter`, `-Y 2024` | Restrict to transactions whose **booking date** falls in this year. Repeatable. Equivalent to passing the years as positional arguments; takes precedence when both are given. |
| `--preview`, `-P` | Non-interactive dry run. Shows a per-bank breakdown of what would be imported, applying only existing rules; never writes files or touches the decision log. |
| `--dry-run` | Run interactively (with prompts) but skip all file writes. |
| `--auto-threshold 0.85` | When matching against existing entries, auto-apply matches scoring above this threshold instead of prompting. |

Examples:

```sh
# Peek at every CSV the importer can see, across all years
uv run bean-import --preview

# See what 2022 + 2023 would look like
uv run bean-import 2022 2023 --preview

# Just process the SPK bank for one year
uv run bean-import 2024 -b spk
```

## Migrating from the old vibe-coded importer

If you have an existing setup with `import_transactions.py` and
`.import_config.json`, the migrator reads the legacy file and writes a
fresh `.beancount-importer/` folder next to it:

```sh
cd ~/finances
bean-import --migrate
```

Output layout:

```
~/finances/
├── import_transactions.py       ← legacy, untouched
├── .import_config.json          ← legacy, read but not modified
├── .beancount-importer/
│   ├── config.toml              ← bank defaults + skip patterns
│   ├── rules.json               ← rules ported from .import_config.json
│   └── tag_state.json           ← active_tag + recent_tags ported
├── documents/                   ← untouched
└── transactions/                ← untouched
```

What gets ported:

- **Rules**: legacy `pattern + match_field` entries become
  `payee_pattern`/`description_pattern` rules. Legacy `match_field="any"` had
  OR semantics, so each one is expanded into two rules sharing a target.
  `default_payee` → `override_payee`, `default_description` → `override_narration`,
  `source_bank` → `bank_key`, `amount_sign="negative"` → `amount_sign="debit"`.
- **Skip patterns**: legacy `skip_update_rules` become top-level
  `[skip_update_patterns]`. `match_field="exact"` entries (date+amount locks)
  are dropped; the new model handles those via the decision log.
- **Suppression flags**: the four legacy global lists
  (`suppress_*_updates_for_rules`) are applied as per-rule flags on rules
  whose pattern matches.
- **Tag state**: `active_tag` + `recent_tags` from the legacy file's `config`
  block become `tag_state.json`.
- **Bank defaults**: only banks with evidence of use (a CSV in `documents/`
  or a `.bean` in `transactions/`) get a section. Defaults reverse-engineered
  from each legacy parser (Sparkasse multi-locale headers + UTF-8-BOM,
  N26 multilingual columns, PayPal `Balance Impact = Memo` filter, etc.).

The migrator is idempotent — any file that already exists in
`.beancount-importer/` is left alone, so you can hand-edit and re-run. An
empty `rules.json` (e.g. from a prior failed run) is treated as absent.

## What gets written where

```
your-project/
├── .beancount-importer/
│   ├── config.toml                # bank definitions, paths, matching tunables
│   ├── rules.json                 # learned rules (regex → target account)
│   ├── decisions.jsonl            # append-only log of one-off categorizations
│   └── tag_state.json             # active trip/event tag, recent tags
├── documents/                     # CSV exports go here
└── transactions/
    └── 2024/
        ├── SPK.bean               # appended to by `import 2024 --bank spk`
        └── N26.bean               ← appended to by `bean-import 2024 --bank n26`
```

The decision log persists user choices for transactions that are *not* rule-driven
(one-off "this specific transfer is a gift") so re-imports replay them
automatically. It is written **immediately** when a decision is made, not
gated on a successful `bean-check` — balance-assertion failures are normal
during multi-account imports and shouldn't erase your manual history.

## Active tags (trips, events)

Tag every transaction during a trip with one tag:

- `always` — applies to every new transaction until cleared
- `once`   — tags the very next transaction, then auto-clears
- `duration` — applies only to transactions whose booking date falls in
  `[from_date, until_date]`

The interactive prompt offers to set/clear active tags. State persists in
`.import_tag_state.json`.

## Architecture

A pure pipeline with iteration-local state. No I/O happens inside the
pipeline beyond CSV/ledger reads and append-only decision logging — all
ledger writes happen in the CLI after `run()` returns. See
[`docs/architecture.md`](docs/architecture.md) for details.

## Development

```sh
uv sync
uv run pytest                # full test suite (enforces 100% coverage)
uv run pytest tests/test_pipeline.py -k year_filter   # one feature
uv run ruff check src tests
uv run pyrefly check src     # type check on production code
```

Type checking is configured for both pyright and pyrefly via
`pyproject.toml`. The `.venv` interpreter is referenced explicitly so LSPs
don't pick the system Python.

Coverage is gated at 100% line + branch on every non-IO module
(`cli.py` and `beancount_io/writer.py` are omitted because they're
interactive / subprocess-driven). Adding code without tests will fail
the test run.

A pre-commit hook runs ruff, pyrefly, and pytest on every commit. To
enable it once per clone:

```sh
uv run pre-commit install
```

## WIP

- audit rule-matching invariances against the reference implementation (some
  rows the reference matches still don't match here)
- internal refactor pass — see [docs/refactor-smells.md](docs/refactor-smells.md)

## Todo

- add more expansive setup and 'help' modes (part of init) to reduce friction of getting started
- pdf ocr input for provenance
- fuzzy picker/filter for full account list (dependency: prompt_toolkit has been evaluated as a good fit before)

