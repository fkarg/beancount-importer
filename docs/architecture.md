# Architecture: `beancount-importer`

_Clean-sheet design for an open-source, modular beancount CSV importer._
_Written 2026-05-04. All decisions finalized._

---

## Goals

- **Zero hardcoded banks.** Adding a new bank should need only a config section; Python code only when the format is truly unusual.
- **Zero private data in the repo.** Account names, file paths, rule patterns all come from user config, not the package.
- **Installable and composable.** `uv add beancount-importer` gives a working CLI; users extend it without forking.
- **Testable from the start.** Every layer has a clean interface; no global state; no interactive prompts in tests.
- **beancount-first I/O.** No hand-rolled `.bean` parsers; beancount v3 AST for reads, its printer for writes.
- **Modular pipeline.** Stages are independently testable. Logic changes don't touch UI code and vice versa.

---

## Non-goals (for v1)

- Web UI.
- Multi-currency portfolio tracking.
- Automatic bank API connections (Plaid, PSD2).
- ML-based categorization.

---

## Feature Reference

Features to preserve from the existing implementation (used as behavioral reference):

| Feature | Module |
|---|---|
| Declarative CSV parsing for standard formats | `parsers/generic.py` |
| Custom parsing for complex formats (PayPal, Telegram) | `parsers/paypal.py`, `parsers/cash.py` |
| German locale dates (`dd.mm.yy`) and amounts (`1.234,56`) | `parsers/locale.py` |
| Fuzzy transaction matching (text similarity + date proximity + SEPA ref) | `matching/scorer.py` |
| Deduplication across existing `.bean` files | `matching/dedup.py` |
| Categorization rules (regex, sign filter, bank filter, field filter) | `rules/` |
| Rule-driven metadata: `actual:`, `settle:`, `amortize:` | `transforms/` |
| Internal transfer detection (reversed sign + amount + date tolerance) | `matching/transfers.py` |
| PayPal cross-referencing | `matching/paypal.py` |
| Interactive merge prompt | `cli.py` (UI layer only) |
| Preview mode (show what would change, no writes) | `pipeline.py` |
| Auto-update mode (apply rule matches without prompting) | `pipeline.py` |
| Confidence-threshold auto-mode | `pipeline.py` |
| Replay mode (record + replay one-off decisions) | `pipeline.py` + `replay.py` |
| Year scaffolding (directory structure, balance assertions) | `scaffolding.py` |
| Rule editor | `cli.py` |
| Document linking (`document` beancount directives) | `scaffolding.py` |
| Active tag support (trip/event tagging) | `rules/tags.py` |
| Post-write `bean-check` validation | `beancount_io/writer.py` |

---

## Package Layout

```
beancount-importer/
├── pyproject.toml
├── README.md
├── docs/
│   └── architecture.md
├── src/
│   └── beancount_importer/
│       ├── __init__.py
│       ├── models.py                # SourceTransaction, LedgerEntry, ImportResult, ProposedChange
│       ├── config.py                # Config (Pydantic), loader from TOML, BankConfig, CsvConfig
│       ├── parsers/
│       │   ├── __init__.py          # build_parser_registry(banks) -> dict[str, AbstractParser]
│       │   ├── base.py              # AbstractParser ABC + Parser Protocol
│       │   ├── generic.py           # GenericCsvParser: config-driven, no bank-specific code
│       │   ├── locale.py            # parse_german_date, parse_german_amount (pure functions)
│       │   ├── paypal.py            # custom (complex reference format, multi-currency)
│       │   └── cash.py              # custom (Telegram JSON log format)
│       ├── rules/
│       │   ├── __init__.py
│       │   ├── models.py            # CategorizationRule, RulesConfig (Pydantic, frozen)
│       │   ├── engine.py            # find_matching_rule, apply_rule -> CategoryProposal
│       │   ├── storage.py           # load/save rules JSON (atomic write)
│       │   └── tags.py              # active tag logic
│       ├── matching/
│       │   ├── __init__.py
│       │   ├── normalize.py         # normalize_text: NFKD + lowercase + whitespace collapse
│       │   ├── scorer.py            # text_similarity, score_candidate, find_candidates
│       │   ├── dedup.py             # DedupKey, make_dedup_key, filter_new
│       │   ├── transfers.py         # find_internal_transfer (cross-bank)
│       │   └── paypal.py            # link_paypal_entries
│       ├── beancount_io/
│       │   ├── __init__.py
│       │   ├── reader.py            # load_ledger_entries via beancount v3 AST
│       │   └── writer.py            # build_transaction, append_entries, splice_all (back-to-front)
│       ├── transforms/
│       │   ├── __init__.py          # TransformHook Protocol + load_transforms registry
│       │   ├── settle.py            # settle: metadata (N days offset)
│       │   ├── actual.py            # actual: metadata (card-swipe vs booking date)
│       │   └── amortize.py          # amortize: metadata generation
│       ├── replay.py                # DecisionLog: record + replay one-off decisions
│       ├── session.py               # ImportSession, ImportOptions (frozen Pydantic)
│       ├── pipeline.py              # pure: run(session, categorize_fn, reporter, decisions)
│       ├── scaffolding.py           # year-dir setup, balance assertions, document links
│       └── cli.py                   # typer app, rich UI, interactive prompts
├── tests/
│   ├── conftest.py
│   ├── fixtures/
│   │   ├── sample.bean
│   │   ├── sample_spk.csv
│   │   ├── sample_n26.csv
│   │   ├── sample_paypal.csv
│   │   └── decisions/
│   │       └── sample_decisions.jsonl    # for replay integration tests
│   ├── test_models.py
│   ├── test_config.py
│   ├── test_parsers.py                   # includes hypothesis-based locale tests
│   ├── test_rules.py
│   ├── test_matching.py
│   ├── test_beancount_io.py
│   ├── test_pipeline.py
│   ├── test_replay.py
│   └── test_transforms.py
└── examples/
    └── mybank/
        ├── README.md
        └── mybank_parser.py              # example custom parser (escape hatch)
```

The `src/` layout prevents importing from the working directory during tests. `uv sync` installs the package in editable mode; no `sys.path` manipulation needed.

---

## Declarative Parser Config (primary mechanism)

Most banks are just a CSV field mapping. They should not require Python code.

Each `[[banks]]` entry in `import_config.toml` is either declarative (has `[banks.csv]`) or custom (has `parser_class`). The parser registry is built entirely from `[[banks]]` — there is no separate `[parsers]` section.

### Declarative bank (no Python needed)

Everything is flat inside one `[banks.csv]` table. Format params (`delimiter`, `date_format`, etc.) sit alongside field mappings (prefixed `field_`) — the prefix eliminates any collision risk with actual CSV column names.

```toml
[[banks]]
key = "spk"
display_name = "Sparkasse"
account = "Assets:B:SPK"
file_glob = "SPK_*.CSV"
output_file = "transactions/{year}/SPK.bean"
source_files = ["transactions/{year}/SPK.bean"]   # defaults to [output_file] if omitted

[banks.csv]
# Format params
delimiter        = ";"
encoding         = "latin-1"
date_format      = ["%d.%m.%y", "%d.%m.%Y"]   # tried in order
amount_locale    = "de"                         # "de" = 1.234,56 | "en" = 1,234.56 | "auto"
header_detection = "auto"                       # "auto" or 0-based row index
skip_zero_amounts = true
skip_row_where   = { "Buchungstext" = "ABSCHLUSS" }  # col: regex; skip matching rows

# Field mappings  (field_<name> = "CSV column name")
field_date             = "Buchungstag"
field_value_date       = "Valutadatum"
field_amount           = "Betrag"
field_currency         = "Waehrung"                           # optional; default "EUR"
field_payee            = "Beguenstigter/Zahlungspflichtiger"
field_description      = ["Verwendungszweck", "Buchungstext"] # list → joined with " | "
field_sepa_reference   = "Kundenreferenz (End-to-End)"
```

```toml
[[banks]]
key = "n26"
display_name = "N26"
account = "Assets:B:N26"
file_glob = "N26_*.csv"
output_file = "transactions/{year}/N26.bean"

[banks.csv]
delimiter         = ","
date_format       = ["%Y-%m-%d"]
amount_locale     = "en"
field_date             = "Booking Date"
field_value_date       = "Value Date"
field_amount           = "Amount (EUR)"
field_payee            = "Partner Name"
field_description      = "Payment Reference"
field_original_amount  = "Original Amount"
field_original_currency = "Original Currency"
field_exchange_rate    = "Exchange Rate"
```

All `field_*` keys map to `SourceTransaction` fields. Unknown `field_*` keys are collected into `raw_data`. This covers Sparkasse, N26, most German banks, and the majority of European bank CSV exports without any Python.

### Custom parser (escape hatch)

For formats that cannot be expressed as a field mapping — PayPal's multi-step reference parsing, Telegram's JSON log, banks that embed account info in data rows:

```toml
[[banks]]
key = "paypal"
display_name = "PayPal"
account = "Assets:B:PayPal"
file_glob = "PayPal_*.csv"
output_file = "transactions/{year}/PayPal.bean"
parser_class = "beancount_importer.parsers.paypal.PayPalParser"
```

`parser_class` is any importable dotted path. `GenericCsvParser` is never instantiated for this bank.

### Parser registry construction

```python
# parsers/__init__.py
def build_parser_registry(banks: list[BankConfig]) -> dict[str, AbstractParser]:
    parsers = {}
    for bank in banks:
        if bank.csv:
            parsers[bank.key] = GenericCsvParser(bank)
        elif bank.parser_class:
            module_path, cls_name = bank.parser_class.rsplit(".", 1)
            cls = getattr(importlib.import_module(module_path), cls_name)
            parsers[bank.key] = cls(bank)   # all parsers receive BankConfig at init
        else:
            raise ConfigError(f"Bank {bank.key!r}: must have [banks.csv] or parser_class")
    return parsers
```

### `AbstractParser` interface

All parsers — generic and custom — are **instances** (not class-based), so `GenericCsvParser` can hold config without dynamic class creation.

```python
# parsers/base.py
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Protocol, runtime_checkable

class AbstractParser(ABC):
    """Inherit from this for custom parsers."""
    HEADER_SIGNATURE: frozenset[str] = frozenset()
    # ^ optional: set of CSV column names; used for format auto-detection (future)

    def __init__(self, bank: "BankConfig") -> None:
        self.bank_name = bank.key
        self.display_name = bank.display_name or bank.key
        self.file_glob = bank.file_glob

    @abstractmethod
    def parse(self, path: Path) -> list["SourceTransaction"]: ...

    def can_parse(self, path: Path) -> bool:
        """Override for format sniffing beyond glob matching."""
        return True

@runtime_checkable
class Parser(Protocol):
    """Structural alternative — duck-typing compatible."""
    bank_name: str
    display_name: str
    file_glob: str
    def parse(self, path: Path) -> list["SourceTransaction"]: ...
    def can_parse(self, path: Path) -> bool: ...
```

`GenericCsvParser` subclasses `AbstractParser` and reads field mappings from `bank.csv`.

---

## Data Models

All models: frozen Pydantic v2. `Decimal` for amounts throughout — no `float`.

`SourceTransaction` (not `CsvTransaction`) — the cash/Telegram parser produces these from JSON, not CSV. The name stays accurate as we add non-CSV sources.

```python
# models.py
from decimal import Decimal
from datetime import date
from typing import Literal, NamedTuple
from pydantic import BaseModel, ConfigDict

class SourceTransaction(BaseModel):
    model_config = ConfigDict(frozen=True)
    booking_date: date
    value_date: date | None = None
    amount: Decimal
    currency: str = "EUR"
    description: str | None = None
    payee: str | None = None
    bank_key: str
    sepa_reference: str = ""
    raw_data: dict = {}
    original_amount: Decimal | None = None
    original_currency: str | None = None
    exchange_rate: Decimal | None = None

class LedgerEntry(BaseModel):
    model_config = ConfigDict(frozen=True)
    date: date
    flag: str = "*"
    payee: str | None = None
    narration: str
    source_account: str
    target_account: str
    amount: Decimal
    currency: str = "EUR"
    metadata: dict[str, str] = {}
    line_start: int = 0
    line_end: int = 0
    file_path: str = ""

class ProposedChange(NamedTuple):
    field: str       # "narration", "payee", "account", "actual", "tag"
    old_val: str
    new_val: str

class Posting(BaseModel):
    """One leg of a transaction. Most proposals have a single non-source posting;
    payroll-style flows produce several (gross / tax / social / net)."""
    model_config = ConfigDict(frozen=True)
    account: str
    amount: Decimal | None = None    # None = beancount-inferred (the "balancing" leg)
    currency: str | None = None      # None = inherit from source_txn
    metadata: dict[str, str] = {}

class CategoryProposal(BaseModel):
    model_config = ConfigDict(frozen=True)
    action: Literal["categorize", "skip", "quit"]
    postings: tuple[Posting, ...] = ()   # additional postings beyond the source-account leg
    payee: str | None = None
    narration: str | None = None
    metadata: dict[str, str] = {}        # transaction-level metadata
    tag: str | None = None
    rule_used: "CategorizationRule | None" = None
    save_as_rule: bool = False           # user asked to persist this as a new rule

    @property
    def target_account(self) -> str:
        """Convenience for the common single-posting case."""
        return self.postings[0].account if self.postings else ""

class ImportResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    source_txn: SourceTransaction
    action: Literal["new", "update", "skip", "transfer", "quit"]
    matched_entry: LedgerEntry | None = None
    proposed_changes: list[ProposedChange] = []
    new_entry_text: str = ""
    rule_matched: "CategorizationRule | None" = None
    is_replay: bool = False              # True if decision came from replay log
    new_rule: "CategorizationRule | None" = None  # rule the user asked to save this turn
    tag_state_delta: "TagStateDelta | None" = None  # active-tag mutation; see Active Tag State
```

**Why a `postings` tuple instead of a single `target_account`:** the original supports payroll-style imports where one CSV row creates multiple legs (gross income, tax, social insurance). A single-account proposal can't express that. The `target_account` property keeps the common case ergonomic.

---

## Config

`import_config.toml` — human-edited, lives alongside `main.bean`, not in this repo. All paths resolve relative to the config file's directory.

```toml
[ledger]
transactions_dir = "transactions"
documents_dir = "documents"
validate_after_write = true        # run bean-check after every write; default true
audit_log = ".import_log.jsonl"    # optional; structured JSONL audit trail
decisions_file = ".import_decisions.jsonl"  # optional; replay log

[[banks]]
# ... see Declarative Parser Config section above

[matching]
min_score = 0.35
min_delta = 0.15
transfer_tolerance_days = 5
transit_account = "Assets:Extern:Transit"
internal_transfer_account_prefixes = ["Assets:B:", "Liabilities:CreditCard:"]

[rules]
file = ".import_rules.json"

[transforms]
enabled = [
    "beancount_importer.transforms.settle",
    "beancount_importer.transforms.actual",
    "beancount_importer.transforms.amortize",
]
```

The `BankConfig` Pydantic model validates bank entries. The `source_files` field (list of paths) defaults to `[output_file]` when omitted, making the read/write split explicit but transparent by default.

Config values are overridable via environment variables through `pydantic-settings`:

```bash
BEANCOUNT_IMPORT_DRY_RUN=1 uv run beancount-import 2025
BEANCOUNT_IMPORT_BANK=spk uv run beancount-import 2025
```

---

## Session State

```python
# session.py
from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings

class ImportOptions(BaseModel):
    model_config = ConfigDict(frozen=True)
    interactive: bool = True
    auto_update: bool = False
    auto_threshold: float | None = None   # score ≥ threshold → no prompt
    skip_existing: bool = False
    no_update: bool = False
    no_import: bool = False
    preview: bool = False
    dry_run: bool = False
    bank_filter: str | None = None

class ImportSession(BaseModel):
    model_config = ConfigDict(frozen=True)
    year: int
    config: Config
    rules: tuple[CategorizationRule, ...]
    tag_state: TagState = TagState()         # loaded from .import_tag_state.json
    options: ImportOptions
```

Mutable outcomes (new rules, replay entries, tag deltas) are returned in `ImportResult` and accumulated by the CLI — never stored in session. The pipeline maintains short-lived working copies during one `run()` call (see *Intra-session state*).

---

## Pipeline

The pipeline is a **pure function**. It reads both sides, computes results, and returns them. It never writes files, never calls `sys.exit`, never touches `stdin`/`stdout` directly (all output goes through `Reporter`).

### Types

```python
# Injected by CLI (interactive) or tests (deterministic).
#
# - candidates: ranked (entry, score) tuples — never just "the best one". The CLI
#   shows all ties and lets the user disambiguate; pure pipelines pick candidates[0]
#   above the auto-threshold or pass them through.
# - account_hints: optional ranked account-name suggestions, e.g. from
#   most-used-account heuristics or an LLM SuggestionProvider. The categorizer is
#   free to ignore them.
class CategorizeContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    txn: SourceTransaction
    rules: tuple[CategorizationRule, ...]
    candidates: tuple[tuple[LedgerEntry, float], ...] = ()
    account_hints: tuple[str, ...] = ()
    active_tag: ActiveTag | None = None     # see Active Tag State

CategorizeFn = Callable[[CategorizeContext], CategoryProposal]

class Reporter(Protocol):
    """All user-visible output goes through here. CLI uses Rich; tests use a no-op."""
    def on_result(self, result: ImportResult) -> None: ...
    def on_progress(self, current: int, total: int, bank: str) -> None: ...
    def on_warning(self, message: str) -> None: ...
    def on_error(self, message: str) -> None: ...

class SuggestionProvider(Protocol):
    """Optional plug-in producing ranked account-name hints for a transaction.
    Implementations: most-used-account, LLM (see llm_suggest.py in original).
    Wired into the CLI's CategorizeFn; the pipeline never calls these directly."""
    def suggest(self, txn: SourceTransaction, rules: list[CategorizationRule]) -> list[str]: ...
```

The single-argument `CategorizeContext` is intentional — it lets us add fields (account hints, candidate disambiguation, tag state) without breaking the `CategorizeFn` signature.

### Flow

```
import_config.toml
        │
        ▼
  Config.load()
        │
  ┌─────┴─────────────────────┐
  │ per bank:                  │
  ▼                            ▼
Parser.parse(csv_path)    reader.load_ledger_entries(source_files)
→ list[SourceTransaction]    → list[LedgerEntry]
  │                            │
  └─────────┬─────────────────┘
            ▼
  DecisionLog.lookup(csv_txn)        ← replay log consulted first
  → CategoryProposal | None
            │
  [None] → Deduplicator.filter(csv_txns, ledger_entries)
            → new: list[SourceTransaction]
              candidates: list[(SourceTransaction, LedgerEntry)]
                    │
           ┌────────┴────────┐
           ▼                 ▼
      new txns          matched txns
           │                 │
    Categorizer          Scorer.score_changes
    (rules → proposal)   (changes list)
           │                 │
    TransformHooks       TransformHooks
           │                 │
           └────────┬────────┘
                    ▼
             list[ImportResult]
                    │
         Reporter.on_result(result)       ← all output here
                    │
            ┌───────┴───────┐
            │               │
        preview         confirmed
        (return)        decisions
                            │
                     Writer.splice_all / append_entries
                            │
                     bean-check validation
                            │
                     DecisionLog.record(result)
```

Cross-bank matching (transfers, PayPal links) runs after all per-bank results, with access to all banks' `LedgerEntry` lists.

### Signature

```python
def run(
    session: ImportSession,
    categorize_fn: CategorizeFn,
    reporter: Reporter,
    decisions: DecisionLog | None = None,
) -> list[ImportResult]: ...
```

### Intra-session state: new rules and active-tag updates

The pipeline is pure, but the *iteration* over transactions is not free of feedback. Two mechanisms keep things clean:

1. **New rules added mid-session must apply to subsequent transactions.** The pipeline maintains a working `rules` list (initialized from `session.rules`). Whenever `categorize_fn` returns a proposal with `save_as_rule=True`, the pipeline derives a `CategorizationRule` from it, **appends it to the working list**, and includes it on the `ImportResult` as `new_rule`. The CLI persists the appended rules to disk after the run (or per-batch, depending on `--auto-update`). Crucially: the *session* is still frozen — the working list is a local pipeline variable, not session state.
2. **Active-tag deltas** (see next section) are returned per result via `tag_state_delta` and applied by the pipeline to its working `ActiveTag` before the next iteration. Persistence happens in the CLI after the run.

This is the only place the pure-pipeline contract bends: the pipeline holds *iteration-local* mutable state (working rules, working tag) that lives for one `run()` call. It still does no I/O and is deterministic given inputs.

### `{year}` template resolution

`output_file` and `source_files` may contain `{year}`. Resolution happens **at pipeline-time** (`session.year` is substituted when the pipeline opens files), not at config-load. This lets one `import_config.toml` serve multiple years and keeps the `Config` object year-agnostic.

---

## Writer: Multi-splice Ordering

This is a correctness invariant. When multiple entries in the same `.bean` file are updated in one session, splicing them front-to-back corrupts all subsequent line numbers. The writer **must** apply splices back-to-front:

```python
# beancount_io/writer.py
def splice_all(path: Path, splices: list[tuple[LedgerEntry, str]], dry_run: bool) -> None:
    """Apply multiple line-range replacements atomically, back-to-front."""
    sorted_splices = sorted(splices, key=lambda s: s[0].line_start, reverse=True)

    backup = path.with_suffix(".bean.bak")
    shutil.copy2(path, backup)
    try:
        lines = path.read_text().splitlines(keepends=True)
        for entry, new_text in sorted_splices:
            lines[entry.line_start : entry.line_end + 1] = [new_text]
        tmp = path.with_suffix(".bean.tmp")
        tmp.write_text("".join(lines))
        if not dry_run:
            tmp.replace(path)
            _run_bean_check(path)   # raises BeanCheckError on failure
            backup.unlink()         # only removed if check passes
        else:
            tmp.unlink()
    except Exception:
        if backup.exists() and not path.exists():
            backup.replace(path)
        raise
```

If `bean-check` fails, the `.bean.bak` is left in place as the recovery file.

---

## Text Normalization

One function, used everywhere in `matching/`:

```python
# matching/normalize.py
import unicodedata, re

def normalize_text(s: str) -> str:
    """NFKD normalize, strip accents, lowercase, collapse whitespace."""
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()
```

Applied in `scorer.py`, `dedup.py`, and `rules/engine.py` before any string comparison. Bank CSVs contain non-breaking spaces, smart quotes, and NFKD-decomposed umlauts — without this, identical transactions produce false non-matches.

---

## Replay Mode

Replay mode records non-trivial one-off decisions so they can be replayed on re-imports without prompting.

### What is recorded

Records decisions that involve explicit user choice that isn't already captured by a categorization rule:
- Manually typed narration or payee (not from a rule)
- Manual tag assignment
- Account choice that wasn't saved as a rule (e.g., "this one time only" decisions

Does **not** record:
- Rule-matched auto-categorizations (the rule already captures the intent)
- New rule creation (stored in the rules file instead)
- Skip decisions
- Replayed decisions (no circular recording)

### Format

`.import_decisions.jsonl` — append-only, one JSON object per line:

```jsonl
{"ts":"2026-05-04T14:30:00Z","session":"abc123","bank":"spk","sig":{"sepa_ref":"NETFLIX-001","hash":"a3f2b1c4"},"decision":{"action":"update","narration":"Netflix Streaming","payee":"Netflix","account":"Expenses:Entertainment","metadata":{},"tag":null}}
{"ts":"2026-05-04T14:31:05Z","session":"abc123","bank":"spk","sig":{"sepa_ref":"","hash":"9d2e1f7a"},"decision":{"action":"new","narration":"Lunch","payee":"Rewe","account":"Expenses:Food:Groceries","metadata":{},"tag":"berlin-trip"}}
```

The signature uses SEPA ref as primary key when present, content hash otherwise — the same strategy as deduplication:

```python
@dataclass(frozen=True)
class DecisionSignature:
    sepa_ref: str | None
    content_hash: str   # sha256(date|amount|payee[:50]|description[:100])
```

### Lookup and recording

```python
# replay.py
class DecisionLog:
    def __init__(self, path: Path | None) -> None: ...

    def lookup(self, txn: SourceTransaction) -> CategoryProposal | None:
        """Return a past decision for this transaction, or None."""
        sig = make_decision_signature(txn)
        return self._index.get(sig)

    def record(self, txn: SourceTransaction, result: ImportResult) -> None:
        """Append a new decision to the log (only for non-trivial, non-replayed results)."""
        if result.is_replay or result.action == "skip":
            return
        if result.rule_matched and not result.rule_matched.save_as_rule:
            return   # rule-driven; don't duplicate
        ...
```

### Use cases

1. **Re-import resilience.** Re-importing a year after changing the output format replays one-off decisions automatically.
2. **Integration tests.** Ship `fixtures/sample_spk.csv` + `fixtures/decisions/sample_decisions.jsonl` + `fixtures/expected/SPK.bean`. Test that `pipeline.run(decisions=log)` produces the expected output deterministically.
3. **Provenance.** The decisions file in git history shows what was changed interactively vs. auto-categorized.
4. **Precedence.** Manual edits to `.bean` files always take precedence — replay only fires when the existing entry hasn't been manually changed since.

### Write ordering: decisions persist independently of `bean-check`

**Decisions are appended to `.import_decisions.jsonl` as soon as the user makes them, before any ledger write.** They are *not* gated on `bean-check` success.

Rationale: `bean-check` failures are routine and often expected — e.g., a cash-account balance assertion that no longer holds because the new import requires a balance update elsewhere. If we only recorded decisions on a clean check, every such failure would erase the user's manual adjustment history and force them to redo all in-session decisions on the next run.

Concrete ordering for one transaction:

1. User makes decision (or replay returns one) → append to `.import_decisions.jsonl` immediately, fsync.
2. Stage the splice/append in memory.
3. After the per-file batch: write the file, run `bean-check`.
4. On `bean-check` failure: leave `.bean.bak` in place, surface the error, **but the decision log already has the user's choices.** Re-running after fixing the balance picks up the same decisions via replay — no rework.

Trade-off: a decision is logged even if its corresponding ledger write was rolled back. That's the right call because the *intent* survives and replays correctly; the alternative (lose intent on every check failure) is worse.

---

## Active Tag State

The original supports trip/event tagging via a stateful "active tag" — once enabled, it auto-applies to subsequent transactions. Three modes:

| Mode | Behavior |
|---|---|
| `always` | Tag every new transaction until cleared. |
| `once` | Tag the next transaction, then clear. |
| `duration` | Tag transactions whose `booking_date` falls in `[from_date, until_date]`. |

### Model

```python
# rules/tags.py
class ActiveTag(BaseModel):
    model_config = ConfigDict(frozen=True)
    tag: str
    mode: Literal["always", "once", "duration"]
    from_date: date | None = None
    until_date: date | None = None

class TagStateDelta(BaseModel):
    """Returned in ImportResult; the pipeline applies it before the next iteration."""
    model_config = ConfigDict(frozen=True)
    op: Literal["set", "clear", "noop"]
    new_state: ActiveTag | None = None       # for "set"

class TagState(BaseModel):
    """Persisted; loaded into ImportSession at start, written by CLI after run()."""
    model_config = ConfigDict(frozen=True)
    active: ActiveTag | None = None
    recent: tuple[str, ...] = ()             # LRU of recently used tags (cap 10)
```

### Persistence

State lives in **`.import_tag_state.json`** (alongside `.import_decisions.jsonl`), not in `categorization_rules.json` and not in `import_config.toml`. Reasoning: it changes session-to-session, often mid-session; rules are edited deliberately, tag state is operational. Keeping them in one file would force users to merge cosmetic state changes whenever they edit rules.

### Threading through the pipeline

Tag state participates in the same iteration-local mutable working state described in *Intra-session state*:

1. Pipeline starts with `working_tag = session.tag_state.active`.
2. Per transaction: pipeline applies `working_tag` to the proposal (subject to `mode`), passes it to `categorize_fn` via `CategorizeContext.active_tag` so the UI can show it.
3. If the categorizer returns `tag_state_delta`, pipeline updates `working_tag` for the next iteration.
4. `mode="once"` is auto-cleared by the pipeline after one tagged transaction (no delta needed from the categorizer).
5. `mode="duration"` is auto-cleared when `booking_date > until_date`.
6. Final `working_tag` and the appended `recent` LRU are written back by the CLI after `run()`.

This keeps the *session* frozen (still pure inputs) while letting the pipeline thread tag changes correctly.

---

## Suppression Flags Model

Whenever a `SourceTransaction` matches an *existing* `LedgerEntry`, the pipeline computes a list of `ProposedChange`s. Suppression flags let users keep a rule that *categorizes new* transactions but doesn't override fields a human has edited on already-imported entries.

Four independent suppression axes, all on `CategorizationRule`:

```python
class CategorizationRule(BaseModel):
    model_config = ConfigDict(frozen=True)
    # ... pattern fields ...
    suppress_updates: bool = False           # skip ALL field updates on matched entries
    suppress_payee_updates: bool = False
    suppress_narration_updates: bool = False
    suppress_account_updates: bool = False
```

Plus a global *skip-update pattern* list at config level, which is more aggressive — it skips the proposal *entirely* (the user isn't even prompted):

```toml
[[rules.skip_update_patterns]]
field = "narration"            # narration | payee | description
pattern = "^Lastschrift "
```

### Precedence

For each matched entry, in order:
1. If any `skip_update_patterns` matches → no `ProposedChange`s emitted, no prompt. (Most aggressive.)
2. Else if matching rule has `suppress_updates=True` → no changes emitted.
3. Else field-level: if `suppress_payee_updates`, drop payee changes; same for narration / account.
4. Remaining changes are presented to `categorize_fn`.

This is the *only* suppression logic — no separate "transaction-level" filter on top. Rule changes propagate, the model stays simple.

---

## Transform Hooks

`transforms/` modules generate metadata (`actual:`, `settle:`, `amortize:`) from rule fields. Each transform implements:

```python
# transforms/__init__.py
class TransformHook(Protocol):
    """Pure: takes the proposal-so-far + context, returns an updated proposal."""
    name: str

    def applies_to(self, rule: CategorizationRule) -> bool:
        """Whether this hook should run for the given rule (e.g., rule has settle_days set)."""
        ...

    def apply(
        self,
        proposal: CategoryProposal,
        txn: SourceTransaction,
        rule: CategorizationRule,
    ) -> CategoryProposal:
        """Return a new CategoryProposal with this hook's contributions added.
        Hooks compose by mutating `metadata`, `postings[*].metadata`, or `narration`.
        Must be deterministic and side-effect-free."""
        ...
```

The pipeline runs all enabled hooks in `config.transforms.enabled` order, after the categorizer has produced the initial proposal. Hooks compose left-to-right: each hook's output is the next hook's input.

`settle.py`, `actual.py`, `amortize.py` each implement this protocol. Adding a new transform = drop a module in `transforms/`, list it in config — no pipeline changes.

---

## Module Contracts

Brief interface description. Implementation details excluded.

### `models.py`
`SourceTransaction`, `LedgerEntry`, `ProposedChange` (NamedTuple), `CategoryProposal`, `ImportResult` — all frozen Pydantic except `ProposedChange`.

### `config.py`
- `Config.load(path: Path) -> Config` — validates with Pydantic; raises `ConfigError` with friendly message on bad input; resolves all paths relative to config file's directory
- `BankConfig` — key, display_name, account, file_glob, output_file, source_files, csv (`CsvConfig | None`), parser_class (`str | None`)
- `CsvConfig` — delimiter, encoding, date_format, amount_locale, header_detection, fields (`CsvFieldMapping`), skip (`CsvSkipConfig`)

### `parsers/generic.py`
- `GenericCsvParser(bank: BankConfig)` — implements `AbstractParser`; reads field mappings from `bank.csv`; handles multi-field description join, list-of-date-formats, header auto-detection
- No bank-specific logic — if a format requires special handling it needs a custom parser

### `parsers/locale.py`
- `parse_german_date(s: str) -> date | None` — pure; handles `dd.mm.yy`, `dd.mm.yyyy`, `yyyy-mm-dd`, `dd/mm/yyyy`; tested with `hypothesis`
- `parse_german_amount(s: str) -> Decimal | None` — pure; handles `1.234,56` and `1,234.56`; tested with `hypothesis`

### `rules/models.py`
- `CategorizationRule` — frozen Pydantic; compiled regex cached in `model_post_init`; all transform fields here (settle_days, add_actual_date, amortize_months, amortize_type); all suppression flags here

### `rules/engine.py`
- `find_matching_rule(txn, rules) -> CategorizationRule | None`
- `apply_rule(txn, rule, existing: LedgerEntry | None) -> CategoryProposal`

### `matching/scorer.py`
- `score_candidate(csv, entry, rules) -> float`
- `find_candidates(csv, entries, rules, *, include_reversed_sign) -> list[tuple[LedgerEntry, float]]`
- All weights are named constants at module top — no inline magic numbers

### `matching/dedup.py`
- `make_dedup_key(txn: SourceTransaction) -> DedupKey`
- `build_existing_key_set(entries: list[LedgerEntry]) -> set[DedupKey]`
- `filter_new(csv_txns, existing_keys) -> list[SourceTransaction]`

### `beancount_io/reader.py`
- `load_ledger_entries(paths: list[Path]) -> list[LedgerEntry]` — accepts `source_files` list; merges and deduplicates; via beancount v3 AST

### `beancount_io/writer.py`
- `build_transaction(entry: LedgerEntry | CategoryProposal, ...) -> str` — beancount v3 printer
- `append_entries(path: Path, texts: list[str], dry_run: bool)` — atomic append
- `splice_all(path: Path, splices: list[tuple[LedgerEntry, str]], dry_run: bool)` — back-to-front, with `.bean.bak` and post-write `bean-check`

### `replay.py`
- `DecisionLog(path: Path | None)` — loads existing log; `None` path → no-op
- `lookup(txn) -> CategoryProposal | None`
- `record(txn, result)` — append-only; skips rule-driven and replayed results

### `pipeline.py`
- `run(session, categorize_fn, reporter, decisions=None) -> list[ImportResult]`
- Pure: no writes, no stdout, no globals
- `categorize_fn` receives `(SourceTransaction, rules, LedgerEntry | None)` — the matched entry is passed so the interactive prompt can show current values

### `cli.py`
- `typer` app; `rich` for all output
- Drives pipeline; collects user decisions; calls writer once at end
- Interactive prompts live here and only here

---

## Test Architecture

```python
# tests/conftest.py — no sys.path manipulation; src/ layout + uv sync handles it
import pytest
from pathlib import Path
from beancount_importer.config import Config, BankConfig, LedgerConfig, MatchingConfig

FIXTURES = Path(__file__).parent / "fixtures"

@pytest.fixture
def sample_bean(tmp_path):
    year_dir = tmp_path / "2024"
    year_dir.mkdir()
    p = year_dir / "SPK.bean"
    p.write_text((FIXTURES / "sample.bean").read_text())
    return p

@pytest.fixture
def sample_config(tmp_path) -> Config:
    return Config(
        ledger=LedgerConfig(transactions_dir=tmp_path, documents_dir=tmp_path / "documents"),
        banks=[BankConfig(
            key="spk", account="Assets:B:SPK", file_glob="SPK_*.CSV",
            output_file="transactions/{year}/SPK.bean",
        )],
        matching=MatchingConfig(),
        rules=RulesConfig(file=tmp_path / ".import_rules.json"),
    )

def deterministic_categorize(
    txn: SourceTransaction,
    rules: list[CategorizationRule],
    existing: LedgerEntry | None,
) -> CategoryProposal:
    """Test double: always categorizes to Expenses:Unknown, never prompts."""
    return CategoryProposal(action="categorize", target_account="Expenses:Unknown")
```

Property-based tests for locale parsing:

```python
# tests/test_parsers.py
from hypothesis import given, strategies as st
from beancount_importer.parsers.locale import parse_german_amount, parse_german_date

@given(st.text())
def test_parse_german_amount_never_raises(s):
    result = parse_german_amount(s)
    assert result is None or isinstance(result, Decimal)

@given(st.text())
def test_parse_german_date_never_raises(s):
    result = parse_german_date(s)
    assert result is None or isinstance(result, date)
```

Test categories:
- **Unit** (`test_parsers.py`, `test_rules.py`, `test_matching.py`): pure functions, no filesystem
- **Integration** (`test_beancount_io.py`, `test_pipeline.py`, `test_replay.py`): use `tmp_path`; no network; no interactive prompts

---

## `pyproject.toml`

```toml
[project]
name = "beancount-importer"
version = "0.1.0"
description = "Modular CSV importer for beancount ledgers"
readme = "README.md"
requires-python = ">=3.11"
license = { text = "MIT" }
dependencies = [
    "beancount>=3.0",
    "pydantic>=2.0",
    "pydantic-settings>=2.0",   # env var overrides for any config field
    "rapidfuzz>=3.0",
    "typer>=0.12",
    "rich>=13.0",
    "tomli-w>=1.0",             # write TOML (for `beancount-import init`)
]

[project.scripts]
beancount-import = "beancount_importer.cli:app"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/beancount_importer"]

[dependency-groups]
dev = [
    "pytest>=8",
    "pytest-cov>=5",
    "hypothesis>=6",   # property-based testing for locale parsers and scorer
    "ruff>=0.9",
    "mypy>=1.10",
]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff.lint]
select = ["E", "F", "UP", "B", "SIM"]

[tool.mypy]
strict = true
```

---

## Adding a Bank (user-facing story)

### Standard CSV format (no Python)

Add a section to `import_config.toml`:

```toml
[[banks]]
key = "dkb"
display_name = "DKB"
account = "Assets:B:DKB"
file_glob = "DKB_*.csv"
output_file = "transactions/{year}/DKB.bean"

[banks.csv]
delimiter     = ";"
date_format   = ["%d.%m.%Y"]
amount_locale = "de"
field_date        = "Buchungstag"
field_amount      = "Betrag (EUR)"
field_payee       = "Gläubiger-ID"
field_description = "Verwendungszweck"
```

`uv run beancount-import 2025 --bank dkb` — done.

### Custom format (Python escape hatch)

```python
# mybank_parser.py  (anywhere on the Python path)
from beancount_importer.parsers.base import AbstractParser
from beancount_importer.models import SourceTransaction

class MyBankParser(AbstractParser):
    def parse(self, path: Path) -> list[SourceTransaction]:
        # custom logic here
        ...
```

```toml
[[banks]]
key = "mybank"
account = "Assets:B:MyBank"
file_glob = "MyBank_*.csv"
output_file = "transactions/{year}/MyBank.bean"
parser_class = "mybank_parser.MyBankParser"
```

---

## Migrating from the Original Vibe-Coded Importer

The reference implementation at `~/finances/import_transactions.py` stores config in `.import_config.json`, which mixes (a) bank/account settings, (b) categorization rules, (c) suppression patterns, and (d) operational state. The new layout splits these:

| Original (`.import_config.json` keys) | New location |
|---|---|
| Bank/account/CSV format settings | `import_config.toml` `[[banks]]` |
| `categorization_rules[]` | `categorization_rules.json` (separate file, schema is `list[CategorizationRule]`) |
| `skip_update_rules[]` | `import_config.toml` `[[rules.skip_update_patterns]]` |
| `suppress_*_for_rules` flags per rule | rule-level booleans on `CategorizationRule` |
| `active_tag` + `recent_tags` | `.import_tag_state.json` |
| One-off interactive decisions (not previously persisted) | `.import_decisions.jsonl` (new) |

Ship a one-shot `beancount-import migrate-from-legacy <path-to-old.json>` that emits the three new files. The migration is read-only of the original; the user reviews and commits.

---

## Implementation Order

| Step | What | Tests added |
|---|---|---|
| 1 | `pyproject.toml` + skeleton (`__init__.py` stubs) | `uv run pytest` → 0 collected, no errors |
| 2 | `models.py` | `test_models.py`: construction, frozen, Decimal, ProposedChange |
| 3 | `config.py` (Config, BankConfig, CsvConfig) | `test_config.py`: load TOML, path resolution, validation errors |
| 4 | `parsers/locale.py` + `parsers/base.py` | `test_parsers.py`: date/amount with hypothesis |
| 5 | `parsers/generic.py` (GenericCsvParser) | `test_parsers.py`: SPK + N26 via config; no hardcoded parsers |
| 6 | `matching/normalize.py` + `matching/dedup.py` | `test_matching.py`: normalization, dedup keys |
| 7 | `matching/scorer.py` | `test_matching.py`: scoring, candidate ranking |
| 8 | `matching/transfers.py` + `matching/paypal.py` | `test_matching.py`: cross-bank matching |
| 9 | `rules/models.py` + `rules/engine.py` + `rules/storage.py` | `test_rules.py`: matching, apply_rule, round-trip storage |
| 10 | `beancount_io/reader.py` | `test_beancount_io.py`: load from sample.bean via AST |
| 11 | `beancount_io/writer.py` (build + splice_all) | `test_beancount_io.py`: format, back-to-front splice, bean-check |
| 12 | `transforms/` | `test_transforms.py`: metadata generation |
| 13 | `replay.py` | `test_replay.py`: lookup, record, integration with fixture decisions |
| 14 | `session.py` + `pipeline.py` + `Reporter` Protocol | `test_pipeline.py`: end-to-end with deterministic_categorize |
| 15 | `cli.py` | Manual smoke test; `--help`; `--preview`; `--dry-run` |
| 16 | Custom parsers: `parsers/paypal.py`, `parsers/cash.py` | Extend `test_parsers.py` |
| 17 | `scaffolding.py` | Manual + integration test |
