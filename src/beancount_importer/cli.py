"""Command-line entry point.

Two subcommands today:
- `import`: load config + rules + tag state, drive the pipeline, write
  the resulting beancount entries, and persist any new rules / tag deltas.
- `init`: write a starter `import_config.toml` next to a fresh `transactions/`
  tree so a user can get going without copying boilerplate.

Interactive prompts live here and only here. The pipeline never reads stdin
or writes to stdout — all user contact funnels through the `categorize_fn`
and `Reporter` injected from this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from beancount_importer.beancount_io.writer import append_entry
from beancount_importer.config import Config
from beancount_importer.models import (
    CategoryProposal,
    ImportResult,
    Posting,
    SourceTransaction,
)
from beancount_importer.pipeline import (
    CategorizeContext,
    run as run_pipeline,
)
from beancount_importer.replay import DecisionLog
from beancount_importer.rules.models import CategorizationRule
from beancount_importer.rules.storage import load_rules, save_rules
from beancount_importer.rules.tags import ActiveTag, TagState
from beancount_importer.session import ImportOptions, ImportSession


app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Modular beancount CSV importer.",
)
console = Console()


# ── Tag-state persistence ─────────────────────────────────────────────────────


def _load_tag_state(path: Path) -> TagState:
    if not path.exists():
        return TagState()
    raw = json.loads(path.read_text(encoding="utf-8"))
    active_data = raw.get("active")
    active = ActiveTag.model_validate(active_data) if active_data else None
    recent = tuple(raw.get("recent", ()))
    return TagState(active=active, recent=recent)


def _save_tag_state(state: TagState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "active": state.active.model_dump(mode="json") if state.active else None,
        "recent": list(state.recent),
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


# ── Reporter ──────────────────────────────────────────────────────────────────


class RichReporter:
    """Reporter that prints one-line progress + summary lines via Rich."""

    def __init__(self) -> None:
        self._last_bank: str = ""

    def on_result(self, result: ImportResult) -> None:
        del result  # individual results are summarized at the end

    def on_progress(self, current: int, total: int, bank: str) -> None:
        if bank != self._last_bank:
            console.log(f"[bold]{bank}[/]: processing transactions…")
            self._last_bank = bank
        del current, total

    def on_warning(self, message: str) -> None:
        console.print(f"[yellow]warning:[/] {message}")

    def on_error(self, message: str) -> None:
        console.print(f"[red]error:[/] {message}")


# ── Interactive categorizer ───────────────────────────────────────────────────


def _render_txn_panel(txn: SourceTransaction) -> Panel:
    parts = [
        f"[bold]{txn.booking_date}[/]  {txn.amount} {txn.currency}",
        f"payee:       {txn.payee or '—'}",
        f"description: {txn.description or '—'}",
        f"bank:        {txn.bank_key}",
    ]
    if txn.sepa_reference:
        parts.append(f"sepa:        {txn.sepa_reference}")
    return Panel("\n".join(parts), title="transaction", border_style="cyan")


def _render_candidates(ctx: CategorizeContext) -> Table | None:
    if not ctx.candidates:
        return None
    table = Table(title="candidates")
    table.add_column("#", justify="right")
    table.add_column("score", justify="right")
    table.add_column("date")
    table.add_column("payee/narration")
    table.add_column("target")
    for i, (entry, score) in enumerate(ctx.candidates, start=1):
        descr = entry.payee or entry.narration
        table.add_row(
            str(i), f"{score:.2f}", str(entry.date), descr, entry.target_account
        )
    return table


def make_preview_categorizer() -> "object":
    """Non-interactive categorizer used by `--preview`.

    Resolution order for the proposal's target account:
      1. matched rule's target — the rule is the authoritative override.
      2. best candidate's target — when an existing entry already has a
         meaningful classification, preserve it; otherwise the diff stage
         would spuriously flag a "change" to Unknown for every previously-
         imported transaction without a rule.
      3. Expenses:Unknown — genuinely-new uncategorized transaction.

    Never prompts and never marks a proposal as `save_as_rule` — preview
    must be a pure read.
    """

    def _fn(ctx: CategorizeContext) -> CategoryProposal:
        if ctx.matched_rule is not None:
            target = ctx.matched_rule.target_account
        elif ctx.candidates:
            target = ctx.candidates[0][0].target_account
        else:
            target = "Expenses:Unknown"
        return CategoryProposal(
            action="categorize",
            postings=(Posting(account=target),),
        )

    return _fn


def make_interactive_categorizer() -> "object":
    """Returns a callable matching `CategorizeFn` that prompts via Rich."""

    def _fn(ctx: CategorizeContext) -> CategoryProposal:
        console.print(_render_txn_panel(ctx.txn))
        candidates_table = _render_candidates(ctx)
        if candidates_table is not None:
            console.print(candidates_table)
        if ctx.matched_rule is not None:
            console.print(
                f"[green]rule match[/]: → {ctx.matched_rule.target_account}"
                + (
                    f"  (payee={ctx.matched_rule.payee_pattern!r})"
                    if ctx.matched_rule.payee_pattern
                    else ""
                )
            )

        action = Prompt.ask(
            "[bold]action[/]",
            choices=["c", "s", "q"],
            default="c",
            show_choices=True,
        )
        if action == "s":
            return CategoryProposal(action="skip")
        if action == "q":
            return CategoryProposal(action="quit")

        default_account = (
            ctx.matched_rule.target_account if ctx.matched_rule else "Expenses:Unknown"
        )
        account = Prompt.ask("target account", default=default_account)
        save_as_rule = False
        if ctx.matched_rule is None:
            save_as_rule = Confirm.ask("save as rule?", default=False)
        return CategoryProposal(
            action="categorize",
            postings=(Posting(account=account),),
            save_as_rule=save_as_rule,
        )

    return _fn


# ── Path resolution ───────────────────────────────────────────────────────────


def _resolve(base: Path, template: str, year: int) -> Path:
    return (base / template.format(year=year)).resolve()


# ── Commands ──────────────────────────────────────────────────────────────────


@app.command("import")
def import_command(
    year: Annotated[
        int | None,
        typer.Argument(
            help="Year used in {year} path templates and as the implicit year-filter. With no YEAR, every parsed transaction is processed and each new entry is filed under transactions/<its-booking-year>/.",
        ),
    ] = None,
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to the importer config TOML"),
    ] = Path(".beancount-importer/config.toml"),
    bank: Annotated[
        str | None,
        typer.Option("--bank", "-b", help="Only process this bank key"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Run interactively but skip all file writes"),
    ] = False,
    preview: Annotated[
        bool,
        typer.Option(
            "--preview",
            "-P",
            help="Non-interactive: show what would happen using only existing rules",
        ),
    ] = False,
    year_filter: Annotated[
        list[int] | None,
        typer.Option(
            "--year-filter",
            "-Y",
            help="Restrict to transactions whose booking date falls in these years (repeatable). Defaults to [year].",
        ),
    ] = None,
    all_years: Annotated[
        bool,
        typer.Option(
            "--all-years",
            help="Disable the implicit year filter — process every transaction the CSV contains.",
        ),
    ] = False,
    auto_threshold: Annotated[
        float | None,
        typer.Option(
            "--auto-threshold",
            help="Score threshold above which matches auto-apply without prompt",
        ),
    ] = None,
) -> None:
    """Run the import and write new beancount entries.

    With no YEAR, every parsed transaction is processed (no year-filter) and
    each new entry is filed under `transactions/<its-booking-year>/`. Pass an
    explicit YEAR to scope both the year-filter and the output folder. The
    `--all-years` flag is still useful when you want to *file* under a
    specific year while pulling in transactions from any year.
    """
    if not config_path.exists():
        console.print(f"[red]config not found:[/] {config_path}")
        raise typer.Exit(code=2)

    config = Config.load(config_path)
    base_dir = config_path.resolve().parent

    rules_path = base_dir / config.rules_file
    decisions_path = base_dir / config.decisions_file
    tags_path = base_dir / config.tag_state_file

    rules = tuple(load_rules(rules_path))
    # Preview mode is a pure read — never write back to the decision log.
    decisions = DecisionLog(None) if preview else DecisionLog(decisions_path)
    tag_state = _load_tag_state(tags_path)

    # `import YEAR` implicitly filters to that year — multi-year CSV exports
    # are common (a 2024 export usually carries late-2023 rows), and the user
    # almost always wants only YEAR's transactions in the YEAR file. With no
    # YEAR (or with `--all-years`), no implicit filter is applied so every
    # parsed row is considered. Explicit `--year-filter` always wins.
    if year_filter:
        effective_year_filter: tuple[int, ...] | None = tuple(year_filter)
    elif all_years or year is None:
        effective_year_filter = None
    else:
        effective_year_filter = (year,)

    options = ImportOptions(
        bank_filter=bank,
        dry_run=dry_run or preview,
        preview=preview,
        interactive=not preview,
        auto_threshold=auto_threshold,
        year_filter=effective_year_filter,
    )
    session = ImportSession(
        year=year,
        config=config,
        rules=rules,
        tag_state=tag_state,
        options=options,
    )

    reporter = RichReporter()
    categorize = (
        make_preview_categorizer() if preview else make_interactive_categorizer()
    )
    results = run_pipeline(session, base_dir, categorize, reporter, decisions=decisions)  # type: ignore[arg-type]

    skip_persist = dry_run or preview
    _persist_results(results, config, base_dir, year, dry_run=skip_persist)
    _persist_new_rules(results, list(rules), rules_path, dry_run=skip_persist)
    _persist_tag_updates(results, tag_state, tags_path, dry_run=skip_persist)
    if preview:
        _print_preview_table(results)
    _print_summary(results, dry_run=skip_persist)


@app.command("init")
def init(
    target: Annotated[
        Path,
        typer.Argument(help="Project directory to scaffold (created if missing)"),
    ] = Path("."),
) -> None:
    """Write a starter `.beancount-importer/config.toml` and a project skeleton.

    Layout produced:
      <target>/.beancount-importer/config.toml   (configuration + state lives here)
      <target>/transactions/                     (per-year .bean files written here)
      <target>/documents/                        (CSV exports go here)
    """
    target.mkdir(parents=True, exist_ok=True)
    config_dir = target / ".beancount-importer"
    config_dir.mkdir(exist_ok=True)
    cfg_path = config_dir / "config.toml"
    if cfg_path.exists():
        console.print(f"[yellow]exists:[/] {cfg_path} (not overwriting)")
    else:
        cfg_path.write_text(_STARTER_CONFIG, encoding="utf-8")
        console.print(f"[green]wrote[/] {cfg_path}")
    (target / "transactions").mkdir(exist_ok=True)
    (target / "documents").mkdir(exist_ok=True)
    console.print(
        f"Edit {cfg_path.relative_to(target)} and add a `[[banks]]` entry per bank."
    )


@app.command("migrate-from-legacy")
def migrate_from_legacy(
    project_dir: Annotated[
        Path,
        typer.Argument(
            help="Directory holding the old import_transactions.py + .import_config.json"
        ),
    ] = Path("."),
) -> None:
    """Migrate a legacy importer setup in place.

    Writes `import_config.toml`, `categorization_rules.json`, and
    `.import_tag_state.json` next to the existing legacy files. Existing files
    are never overwritten — re-run safely after hand-edits.
    """
    if not project_dir.exists():
        console.print(f"[red]not found:[/] {project_dir}")
        raise typer.Exit(code=2)
    from beancount_importer.scaffolding import migrate_legacy

    migrate_legacy(project_dir, console=console)


# ── Result persistence ────────────────────────────────────────────────────────


def _persist_results(
    results: list[ImportResult],
    config: Config,
    base_dir: Path,
    year: int | None,
    *,
    dry_run: bool,
) -> None:
    """Append each new entry to its bank's output file.

    When `year` is given, the same `output_file.format(year=year)` path is used
    for every txn in the batch (the explicit-year flow). When `year` is None
    (no-year flow), each txn is filed under its own `booking_date.year` so a
    single sweep across multiple years lands in the correct per-year files.
    """
    for r in results:
        if r.action != "new" or not r.new_entry_text:
            continue
        try:
            bank_cfg = config.bank(r.source_txn.bank_key)
        except KeyError:
            continue
        target_year = year if year is not None else r.source_txn.booking_date.year
        out_path = _resolve(base_dir, bank_cfg.output_file, target_year)
        append_entry(r.new_entry_text, out_path, dry_run=dry_run)


def _persist_new_rules(
    results: list[ImportResult],
    existing: list[CategorizationRule],
    rules_path: Path,
    *,
    dry_run: bool,
) -> None:
    new_rules = [r.new_rule for r in results if r.new_rule is not None]
    if not new_rules or dry_run:
        return
    save_rules(existing + new_rules, rules_path)


def _persist_tag_updates(
    results: list[ImportResult],
    tag_state: TagState,
    tags_path: Path,
    *,
    dry_run: bool,
) -> None:
    state = tag_state
    for r in results:
        delta = r.tag_state_delta
        if delta is None or delta.op == "noop":
            continue
        if delta.op == "set":
            state = state.with_active(delta.new_state)
        elif delta.op == "clear":
            state = state.with_active(None)
    if dry_run:
        return
    if state != tag_state:
        _save_tag_state(state, tags_path)


@dataclass
class _PreviewStats:
    total: int = 0
    matched: int = 0  # already in ledger or would update with no actual changes
    update_auto: int = 0
    update_manual: int = 0
    import_auto: int = 0
    import_manual: int = 0

    @property
    def update_total(self) -> int:
        return self.update_auto + self.update_manual

    @property
    def import_total(self) -> int:
        return self.import_auto + self.import_manual

    def add(self, other: "_PreviewStats") -> None:
        self.total += other.total
        self.matched += other.matched
        self.update_auto += other.update_auto
        self.update_manual += other.update_manual
        self.import_auto += other.import_auto
        self.import_manual += other.import_manual


def _aggregate_preview(results: list[ImportResult]) -> dict[str, _PreviewStats]:
    by_bank: dict[str, _PreviewStats] = {}
    for r in results:
        bank = r.source_txn.bank_key
        s = by_bank.setdefault(bank, _PreviewStats())
        s.total += 1
        if r.action == "skip":
            s.matched += 1
        elif r.action == "update":
            # An "update" with no actual proposed changes is just a match
            # against an already-imported entry. The legacy preview rolled
            # these into already-matched; do the same so the counts mean
            # what they look like.
            if not r.proposed_changes:
                s.matched += 1
            elif r.rule_matched is not None:
                s.update_auto += 1
            else:
                s.update_manual += 1
        elif r.action == "new":
            if r.rule_matched is not None:
                s.import_auto += 1
            else:
                s.import_manual += 1
    return by_bank


def _print_preview_table(results: list[ImportResult]) -> None:
    """Render a per-bank breakdown matching the legacy importer's style:
    indented hierarchy with absolute counts and percentages, plus colors that
    flag manual work (cyan), auto-applied (green/yellow), and unmatched bean
    entries when relevant. Per-bank section followed by a TOTAL when more
    than one bank is in play.
    """
    if not results:
        return

    by_bank = _aggregate_preview(results)
    totals = _PreviewStats()
    for s in by_bank.values():
        totals.add(s)

    def pct(n: int, denom: int) -> str:
        return f"({n / denom * 100:5.1f}%)" if denom else "(  0.0%)"

    def section(label: str, s: _PreviewStats) -> None:
        console.print(f"\n  [bold]{label}[/]")
        denom = s.total
        console.print(
            f"    CSV transactions:     [bold]{s.total:4d}[/]  [dim]{pct(s.total, denom)}[/]"
        )
        console.print(
            f"    Already matched:      {s.matched:4d}  [dim]{pct(s.matched, denom)}[/]"
        )
        if s.update_total:
            console.print(
                f"    Would update:         [yellow]{s.update_total:4d}[/]  [yellow]{pct(s.update_total, denom)}[/]"
            )
            if s.update_auto:
                console.print(
                    f"      - Auto (rule):      [yellow]{s.update_auto:4d}[/]  [yellow]{pct(s.update_auto, denom)}[/]"
                )
            if s.update_manual:
                console.print(
                    f"      - Manual:           [cyan]{s.update_manual:4d}[/]  [cyan]{pct(s.update_manual, denom)}[/]"
                )
        if s.import_total:
            console.print(
                f"    Would import:         [green]{s.import_total:4d}[/]  [green]{pct(s.import_total, denom)}[/]"
            )
            if s.import_auto:
                console.print(
                    f"      - Auto (rule):      [green]{s.import_auto:4d}[/]  [green]{pct(s.import_auto, denom)}[/]"
                )
            if s.import_manual:
                console.print(
                    f"      - Manual:           [cyan]{s.import_manual:4d}[/]  [cyan]{pct(s.import_manual, denom)}[/]"
                )

    rule = "  " + "─" * 66
    console.print(rule)
    console.print(f"  [bold]Preview[/]")
    for bank in sorted(by_bank):
        section(bank.upper(), by_bank[bank])
    if len(by_bank) > 1:
        section("TOTAL", totals)

    manual = totals.update_manual + totals.import_manual
    auto = totals.update_auto + totals.import_auto
    console.print()
    if manual:
        console.print(f"  [cyan]→ {manual} transaction(s) need manual attention[/]")
    if auto:
        console.print(f"  [green]→ {auto} transaction(s) would auto-apply via rules[/]")
    console.print(rule)


def _print_summary(results: list[ImportResult], *, dry_run: bool) -> None:
    counts: dict[str, int] = {}
    for r in results:
        # An "update" with no proposed changes means the txn matches an
        # existing entry verbatim — present it as a skip so the headline
        # counts agree with the per-bank breakdown.
        action = r.action
        if action == "update" and not r.proposed_changes:
            action = "skip"
        counts[action] = counts.get(action, 0) + 1
    rendered = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    prefix = "[yellow]dry-run[/] " if dry_run else ""
    console.print(f"\n{prefix}done: {rendered or 'no transactions'}")


_STARTER_CONFIG = """\
# Starter configuration for beancount-importer.
# All paths in this file are resolved relative to the config file's directory
# (i.e. .beancount-importer/), so `../transactions` and `../documents` point
# to siblings of that folder.
# Add one [[banks]] section per bank. {year} is substituted at run time.

rules_file = "rules.json"
decisions_file = "decisions.jsonl"
tag_state_file = "tag_state.json"
documents_dir = "../documents"
transactions_dir = "../transactions"

[matching]
min_score = 0.35

# [[banks]]
# key = "spk"
# display_name = "Sparkasse"
# account = "Assets:B:SPK"
# file_glob = "SPK_*.csv"
# output_file = "../transactions/{year}/SPK.bean"
#
# [banks.csv]
# delimiter = ";"
# date_format = ["%d.%m.%y", "%d.%m.%Y"]
# amount_locale = "de"
# field_date = "Buchungstag"
# field_amount = "Betrag"
# field_currency = "Waehrung"
# field_payee = "Beguenstigter"
# field_description = "Verwendungszweck"
# field_sepa_reference = "Kundenreferenz"
"""


if __name__ == "__main__":
    app()
