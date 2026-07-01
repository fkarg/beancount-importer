"""Command-line entry point.

One command: `bean-import`. Pass no flags to run the import interactively.
Use `--init` to scaffold a new project, `--migrate` to migrate a legacy setup.

Interactive prompts live here and only here. The pipeline never reads stdin
or writes to stdout — all user contact funnels through the `categorize_fn`
and `Reporter` injected from this module.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from beancount_importer import __version__, source_hash
from beancount_importer.beancount_io.writer import append_entry, apply_update
from beancount_importer.config import Config
from beancount_importer.models import (
    CategoryProposal,
    ImportResult,
    Posting,
)
from beancount_importer.pipeline import (
    BeanProvenanceStats,
    CategorizeContext,
    CategorizeFn,
    compute_bean_provenance_stats,
    run as run_pipeline,
)
from beancount_importer.categorizer.host import (
    make_screen_categorizer,
    make_screen_merge_fn,
)
from beancount_importer.replay import DecisionLog
from beancount_importer.rules.models import CategorizationRule
from beancount_importer.rules.storage import load_rules, save_rules
from beancount_importer.rules.tags import ActiveTag, RememberedTag, TagState
from beancount_importer.session import ImportOptions, ImportSession


app = typer.Typer(
    no_args_is_help=False,
    add_completion=False,
    help="Modular beancount CSV importer.",
)


def _version_callback(value: bool) -> None:
    """Print "bean-import <version> (<source-hash>)" and exit.

    Install-time metadata version + live source-tree hash. The version
    comes from the installed .dist-info (snapshotted by uv/pip at
    install time) and can be stale for editable installs. The src hash,
    by contrast, is computed from the .py files Python is currently
    importing — so if the two don't match a known-good reference, the
    user can tell at a glance whether the running code matches what
    they think they have installed.
    """
    if not value:
        return
    typer.echo(f"bean-import {__version__} ({source_hash()})")
    raise typer.Exit()



# `emoji=False` is essential: account paths like `Assets:B:SPK` and
# `Liabilities:CreditCard:Visa` contain `:X:` substrings that Rich's emoji
# subsystem would otherwise rewrite into emoji glyphs.
console = Console(highlight=False, emoji=False)


# ── Tag-state persistence ─────────────────────────────────────────────────────


def _load_tag_state(path: Path) -> TagState:
    if not path.exists():
        return TagState()
    raw = json.loads(path.read_text(encoding="utf-8"))
    active_data = raw.get("active")
    active = ActiveTag.model_validate(active_data) if active_data else None
    recent = tuple(_parse_remembered(r) for r in raw.get("recent", ()))
    return TagState(active=active, recent=recent)


def _parse_remembered(raw) -> RememberedTag:
    """Parse one `recent` entry, tolerating the legacy bare-string format.

    Old state (and the reference's `recent_tags`) stored plain names with no
    window; those become name-only remembered tags.
    """
    if isinstance(raw, str):
        return RememberedTag(tag=raw)
    return RememberedTag.model_validate(raw)


def _save_tag_state(state: TagState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "active": state.active.model_dump(mode="json") if state.active else None,
        "recent": [r.model_dump(mode="json") for r in state.recent],
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


# ── Reporter ──────────────────────────────────────────────────────────────────


class RichReporter:
    """Reporter that prints a one-line ticker per finalised result.

    The ticker is the per-decision feedback the design doc calls for:
    every transaction's outcome appears in scrollback as it happens, so
    the user knows where the run is without waiting for the summary.
    `format_line` picks the glyph + suffix from the result's action, and
    we print the markup string directly — append-only, no `Live` region.

    `quiet=True` suppresses the per-row ticker. Used by `--preview`,
    where the run produces a final summary table that already shows
    every row's classification — the per-row ticker doubles the noise
    without adding information. The bank-progress log still fires so
    the user sees the run isn't hung.
    """

    def __init__(self, *, quiet: bool = False, chronological: bool = False) -> None:
        self._last_bank: str = ""
        self._last_date: date | None = None
        self._quiet = quiet
        self._chronological = chronological

    def on_result(self, result: ImportResult) -> None:
        if self._quiet:
            return
        from beancount_importer.categorizer.ticker import format_line

        console.print(format_line(result))

    def on_progress(
        self, current: int, total: int, bank: str, booking_date: date
    ) -> None:
        # In `--time` mode the stream is interleaved across banks, so a
        # per-bank header would fire almost every row. Chunk by day instead;
        # the per-row ticker already carries each row's bank.
        if self._chronological:
            if booking_date != self._last_date:
                console.log(f"[bold]─── {booking_date.isoformat()} ───[/]")
                self._last_date = booking_date
        elif bank != self._last_bank:
            console.log(f"[bold]{bank}[/]: processing transactions…")
            self._last_bank = bank
        del current, total

    def on_error(self, message: str) -> None:
        console.print(f"[red]error:[/] {message}")


# ── Interactive categorizer ───────────────────────────────────────────────────


def make_preview_categorizer() -> CategorizeFn:
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




# ── Path resolution ───────────────────────────────────────────────────────────


def _resolve(base: Path, template: str, year: int) -> Path:
    return (base / template.format(year=year)).resolve()


# ── Init / migrate helpers ────────────────────────────────────────────────────


def _run_init(target: Path) -> None:
    """Write a starter `.beancount-importer/config.toml` and project skeleton.

    Layout produced:
      <target>/.beancount-importer/config.toml
      <target>/transactions/
      <target>/documents/
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


def _run_migrate(project_dir: Path) -> None:
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


# ── Main command ──────────────────────────────────────────────────────────────


@app.command()
def main(
    years: Annotated[
        list[int] | None,
        typer.Argument(
            help="Years to process. With none, every parsed transaction is considered.",
        ),
    ] = None,
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to the importer config TOML"),
    ] = Path(".beancount-importer/config.toml"),
    root: Annotated[
        Path | None,
        typer.Option(
            "--root",
            "-R",
            help=(
                "Override the finances root used to resolve relative paths in the "
                "config. Defaults to the parent of `.beancount-importer/` when the "
                "config lives there, otherwise the current working directory."
            ),
        ),
    ] = None,
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
            help="Restrict to transactions whose booking date falls in these years. Takes precedence over positional years when both are given.",
        ),
    ] = None,
    auto_threshold: Annotated[
        float | None,
        typer.Option(
            "--auto-threshold",
            help="Score threshold above which matches auto-apply without prompt",
        ),
    ] = None,
    time: Annotated[
        bool,
        typer.Option(
            "--time",
            help="Review transactions interleaved across banks in date order (chunked by day) instead of bank-by-bank",
        ),
    ] = False,
    init: Annotated[
        bool,
        typer.Option(
            "--init",
            help="Write a starter config and project skeleton in the current directory, then exit",
        ),
    ] = False,
    migrate: Annotated[
        bool,
        typer.Option(
            "--migrate",
            help="Migrate a legacy importer setup in the current directory, then exit",
        ),
    ] = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Print version (with source-tree hash) and exit",
        ),
    ] = False,
) -> None:
    """Import CSV transactions into beancount ledger files.

    Examples:
        bean-import                          # all years, all banks
        bean-import 2024                     # just 2024
        bean-import 2022 2023 --preview      # peek without writing
        bean-import 2024 --bank spk          # one bank, one year
        bean-import --init                   # scaffold a new project
        bean-import --migrate                # migrate legacy setup
    """
    del version  # handled via callback; only present in signature for --help

    if init:
        _run_init(Path("."))
        return

    if migrate:
        _run_migrate(Path("."))
        return

    if not config_path.exists():
        console.print(f"[red]config not found:[/] {config_path}")
        raise typer.Exit(code=2)

    config = Config.load(config_path)
    base_dir = _resolve_finances_root(config_path, root)
    _validate_paths(config, config_path, base_dir)

    rules_path = base_dir / config.rules_file
    decisions_path = base_dir / config.decisions_file
    tags_path = base_dir / config.tag_state_file

    rules = tuple(load_rules(rules_path))
    decisions = DecisionLog(None) if preview else DecisionLog(decisions_path)
    tag_state = _load_tag_state(tags_path)

    if year_filter:
        effective_year_filter: tuple[int, ...] | None = tuple(year_filter)
    elif years:
        effective_year_filter = tuple(years)
    else:
        effective_year_filter = None

    options = ImportOptions(
        bank_filter=bank,
        dry_run=dry_run or preview,
        preview=preview,
        interactive=not preview,
        auto_threshold=auto_threshold,
        chronological=time,
        year_filter=effective_year_filter,
    )
    session = ImportSession(
        config=config,
        rules=rules,
        tag_state=tag_state,
        options=options,
    )

    reporter = RichReporter(quiet=preview, chronological=options.chronological)
    categorize: CategorizeFn = (
        make_preview_categorizer()
        if preview
        else make_screen_categorizer(console)
    )
    # Preview is non-interactive; auto-merge mirrors today's behaviour by
    # passing no `merge_fn`. Real interactive runs get Screen 3 wired up.
    merge_fn = None if preview else make_screen_merge_fn(console)
    # Accumulator the pipeline appends to mid-run. On Ctrl+C the pipeline
    # raises out, but we keep this list so we can still persist the rules
    # the user created before bailing.
    results: list[ImportResult] = []
    skip_persist = dry_run or preview
    try:
        run_pipeline(
            session,
            base_dir,
            categorize,
            reporter,
            decisions=decisions,
            merge_fn=merge_fn,
            results_accumulator=results,
        )
    except KeyboardInterrupt:
        # Ctrl+C: drop the buffered decisions and any pending .bean
        # writes (results were never persisted). Rules are different —
        # the user pressing [r] is durable intent that should outlive
        # a rage-quit, so we still persist whatever rules accumulated.
        if not skip_persist:
            _persist_new_rules(results, list(rules), rules_path, dry_run=False)
        new_rule_count = sum(1 for r in results if r.new_rule is not None)
        suffix = (
            f" (kept {new_rule_count} new rule{'s' if new_rule_count != 1 else ''})"
            if new_rule_count and not skip_persist
            else ""
        )
        console.print(f"\n[yellow]interrupted — no decisions saved{suffix}[/]")
        raise typer.Exit(code=130) from None

    # Natural completion (or `[q] quit` mid-run, which breaks the loop
    # cleanly inside `run_pipeline`). Flush decisions FIRST so the
    # user's manual choices survive any subsequent file-write failure
    # — the original "decisions are durable before .bean" contract.
    if not skip_persist:
        decisions.flush()
    _persist_results(results, config, base_dir, dry_run=skip_persist)
    _persist_new_rules(results, list(rules), rules_path, dry_run=skip_persist)
    _persist_tag_updates(results, tag_state, tags_path, dry_run=skip_persist)
    if preview:
        bean_stats = compute_bean_provenance_stats(session, base_dir)
        _print_preview_table(results, config, bean_stats)
    else:
        bean_stats = compute_bean_provenance_stats(session, base_dir)
        _print_summary(results, config, bean_stats, dry_run=skip_persist)
    if not skip_persist:
        _run_ledger_check(config, base_dir, results)


# ── Result persistence ────────────────────────────────────────────────────────


# Cap on how many bean-check output lines we echo, so a badly-broken ledger
# doesn't bury the summary under thousands of lines.
_MAX_CHECK_LINES = 40


def _run_ledger_check(
    config: Config, base_dir: Path, results: list[ImportResult]
) -> None:
    """Post-write validation: run `bean-check` against the top-level ledger.

    Non-fatal — the import is already written; this only informs. Unlike the
    per-fragment splice check (syntax-only, since a fragment has no account
    `open`s), this validates the *whole* `main_bean` ledger — accounts,
    balances, plugins — because partial checks don't mean much. Uses the
    user's `bean-check` on PATH (their plugins/env); skipped when `main_bean`
    is unset or `bean-check` isn't installed.
    """
    if not config.main_bean or shutil.which("bean-check") is None:
        return
    years = {r.source_txn.booking_date.year for r in results} or {date.today().year}
    for path in sorted({_resolve(base_dir, config.main_bean, y) for y in years}):
        if not path.exists():
            continue
        proc = subprocess.run(
            ["bean-check", str(path)], capture_output=True, text=True
        )
        if proc.returncode == 0:
            console.print(f"[green]✓[/] bean-check {path.name}: clean")
            continue
        lines = [ln for ln in proc.stderr.splitlines() if ln.strip()]
        console.print(
            f"[yellow]⚠ bean-check[/] found issues in {path.name} "
            f"[dim](not blocking — already written)[/]:"
        )
        for ln in lines[:_MAX_CHECK_LINES]:
            console.print(f"  [yellow]{ln}[/]")
        if len(lines) > _MAX_CHECK_LINES:
            console.print(f"  [dim]… {len(lines) - _MAX_CHECK_LINES} more line(s)[/]")


def _persist_results(
    results: list[ImportResult],
    config: Config,
    base_dir: Path,
    *,
    dry_run: bool,
) -> None:
    """Persist each result to disk: append "new" entries, splice "update" entries.

    Entries route by the source transaction's booking year. Skipped/quit
    actions are no-ops. Updates without proposed_changes are silent — the
    matched entry is already correct, no rewrite needed.
    """
    for r in results:
        try:
            bank_cfg = config.bank(r.source_txn.bank_key)
        except KeyError:
            continue
        if r.action == "new" and r.new_entry_text:
            out_path = _resolve(
                base_dir, bank_cfg.output_file, r.source_txn.booking_date.year
            )
            append_entry(r.new_entry_text, out_path, dry_run=dry_run)
        elif (
            r.action == "update"
            and r.matched_entry is not None
            and r.proposal is not None
            and r.proposed_changes
        ):
            apply_update(
                r.matched_entry,
                r.proposal,
                bank_cfg.account,
                dry_run=dry_run,
                narration_max_length=config.narration_max_length,
            )


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
        if delta.op == "set" and delta.new_state is not None:
            # Record both the active tag AND its window in `recent`, so the
            # picker can re-offer it (and pre-fill its dates) next time.
            state = state.with_active(delta.new_state).with_recent(delta.new_state)
        elif delta.op == "clear":
            state = state.with_active(None)
    if dry_run:
        return
    if state != tag_state:
        _save_tag_state(state, tags_path)


@dataclass
class _PreviewStats:
    total: int = 0
    matched: int = 0
    would_skip_rule: int = 0
    update_auto: int = 0
    update_manual: int = 0
    import_auto: int = 0
    import_manual: int = 0
    # Reverse-matching: ledger entries observed for this bank in this year,
    # the subset with no CSV provenance, and the plugin-expanded count from
    # `bean-query` (zero when not collected).
    total_in_bean: int = 0
    bean_unmatched: int = 0
    bean_expanded: int = 0

    @property
    def update_total(self) -> int:
        return self.update_auto + self.update_manual

    @property
    def import_total(self) -> int:
        return self.import_auto + self.import_manual

    @property
    def manual_work(self) -> int:
        return self.update_manual + self.import_manual

    @property
    def auto_work(self) -> int:
        return self.update_auto + self.import_auto

    def add(self, other: _PreviewStats) -> None:
        """Sum CSV-side counts only. Bean-side `total_in_bean` /
        `bean_unmatched` are NOT accumulated here: a single ledger
        transaction touching two bank accounts is loaded once per bank and
        would be double-counted at year/overall level. The year aggregate
        (computed in `compute_bean_provenance_stats` keyed by ``("", year)``)
        is injected at year totals instead. `bean_expanded` is summed across
        banks despite a similar caveat — the year-level deduped expanded
        count isn't available, and matching the reference's behaviour is
        more useful than dropping the row.
        """
        self.total += other.total
        self.matched += other.matched
        self.would_skip_rule += other.would_skip_rule
        self.update_auto += other.update_auto
        self.update_manual += other.update_manual
        self.import_auto += other.import_auto
        self.import_manual += other.import_manual
        self.bean_expanded += other.bean_expanded


def _aggregate_preview(
    results: list[ImportResult],
    bean_stats: dict[tuple[str, int], BeanProvenanceStats],
    year: int,
    config: Config,
) -> dict[str, _PreviewStats]:
    """Bucket counts by section identifier.

    A section is either a configured bank's account (`Assets:B:SPK`) or, for
    bean entries with no configured bank, the relative path of the `.bean`
    file they live in. CSV-side rows always map to a bank → account; bean-side
    keys come pre-bucketed from `compute_bean_provenance_stats`.
    """
    by_section: dict[str, _PreviewStats] = {}
    bank_account = {b.key: b.account for b in config.banks}
    for r in results:
        section = bank_account.get(r.source_txn.bank_key, r.source_txn.bank_key)
        s = by_section.setdefault(section, _PreviewStats())
        s.total += 1
        if r.action == "skip":
            if r.skip_reason == "skip_rule":
                s.would_skip_rule += 1
            else:
                s.matched += 1
        elif r.action == "update":
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

    # Fold in bean-side stats for every section with ledger entries this year,
    # including file sections. The year-aggregate sentinel (section=="") is
    # handled separately at year-total level by the caller.
    for (section, bean_year), prov in bean_stats.items():
        if bean_year != year or section == "":
            continue
        s = by_section.setdefault(section, _PreviewStats())
        s.total_in_bean = prov.total_in_bean
        s.bean_unmatched = prov.bean_unmatched
        s.bean_expanded = prov.bean_expanded
    return by_section


def _section_label(section_id: str) -> str:
    """Display label for a section.

    Configured-bank sections are keyed by the bank's account (`Assets:B:SPK`
    → `SPK`); file sections are keyed by the file's relative path
    (`TR.bean` → `TR`, `2022-01.bean` → `2022-01`).
    """
    if ":" in section_id:
        return section_id.rsplit(":", 1)[-1].upper()
    return Path(section_id).stem


def _ordered_sections(by_section: dict[str, _PreviewStats], config: Config) -> list[str]:
    """Configured banks first (config order), then file sections alphabetical.

    The familiar SPK/N26/PAYPAL ordering is preserved; non-configured ledger
    files (TR.bean, 2022-01.bean, …) cluster at the bottom of each year.
    """
    configured = [b.account for b in config.banks if b.account in by_section]
    seen = set(configured)
    extras = sorted(s for s in by_section if s not in seen)
    return configured + extras


def _print_preview_table(
    results: list[ImportResult],
    config: Config,
    bean_stats: dict[tuple[str, int], BeanProvenanceStats] | None = None,
) -> None:
    bean_stats = bean_stats or {}
    if not results and not bean_stats:
        return

    by_year: dict[int, list[ImportResult]] = {}
    for r in results:
        by_year.setdefault(r.source_txn.booking_date.year, []).append(r)
    # Years that only show up via bean-side data (e.g., the user is
    # checking last year's ledger after CSVs were already imported).
    for (_, bean_year) in bean_stats:
        by_year.setdefault(bean_year, [])

    def pct(n: int, denom: int) -> str:
        return f"({n / denom * 100:5.1f}%)" if denom else "(  0.0%)"

    def section(
        label: str,
        s: _PreviewStats,
        indent: str = "  ",
        *,
        show_rollup: bool = False,
    ) -> None:
        console.print(f"\n{indent}[bold]{label}[/]")
        denom = s.total
        body = indent + "  "
        console.print(
            f"{body}CSV transactions:     [bold]{s.total:4d}[/]  [dim]{pct(s.total, denom)}[/]"
        )
        console.print(
            f"{body}Already matched:      {s.matched:4d}  [dim]{pct(s.matched, denom)}[/]"
        )
        if s.would_skip_rule:
            console.print(
                f"{body}Skip by rule:         {s.would_skip_rule:4d}  [dim]{pct(s.would_skip_rule, denom)}[/]"
            )
        if s.update_total:
            console.print(
                f"{body}Would update:         [yellow]{s.update_total:4d}[/]  [yellow]{pct(s.update_total, denom)}[/]"
            )
            if s.update_auto:
                console.print(
                    f"{body}  - Auto (rule):      [yellow]{s.update_auto:4d}[/]  [yellow]{pct(s.update_auto, denom)}[/]"
                )
            if s.update_manual:
                console.print(
                    f"{body}  - Manual:           [cyan]{s.update_manual:4d}[/]  [cyan]{pct(s.update_manual, denom)}[/]"
                )
        if s.import_total:
            console.print(
                f"{body}Would import:         [green]{s.import_total:4d}[/]  [green]{pct(s.import_total, denom)}[/]"
            )
            if s.import_auto:
                console.print(
                    f"{body}  - Auto (rule):      [green]{s.import_auto:4d}[/]  [green]{pct(s.import_auto, denom)}[/]"
                )
            if s.import_manual:
                console.print(
                    f"{body}  - Manual:           [cyan]{s.import_manual:4d}[/]  [cyan]{pct(s.import_manual, denom)}[/]"
                )

        if s.total_in_bean or s.bean_unmatched or s.bean_expanded:
            console.print(f"{body}[dim]---[/]")
            if s.bean_expanded and s.bean_expanded != s.total_in_bean:
                console.print(
                    f"{body}Expanded:             {s.bean_expanded:4d}"
                )
            if s.total_in_bean:
                console.print(
                    f"{body}Transactions:         {s.total_in_bean:4d}  [dim](100.0%)[/]"
                )
            if show_rollup:
                # Rollups span CSV-side and bean-side work; denom is the
                # larger of the two so percentages stay <=100% regardless of
                # which side dominates.
                rollup_denom = max(s.total, s.total_in_bean)
                if s.manual_work:
                    console.print(
                        f"{body}Manual attention:     [cyan]{s.manual_work:4d}[/]  [cyan]{pct(s.manual_work, rollup_denom)}[/]"
                    )
                if s.auto_work:
                    console.print(
                        f"{body}Auto (rule):          [green]{s.auto_work:4d}[/]  [green]{pct(s.auto_work, rollup_denom)}[/]"
                    )
            if s.bean_unmatched:
                bean_denom = s.total_in_bean
                console.print(
                    f"{body}No CSV source:        [magenta]{s.bean_unmatched:4d}[/]  [magenta]{pct(s.bean_unmatched, bean_denom)}[/]"
                )

    rule = "  " + "─" * 66
    overall = _PreviewStats()
    console.print(rule)
    console.print("  [bold]Preview[/]")

    for year in sorted(by_year):
        console.print(f"\n  [bold magenta]── {year} ──[/]")
        year_results = by_year[year]
        by_section = _aggregate_preview(year_results, bean_stats, year, config)
        year_total = _PreviewStats()
        for section_id in _ordered_sections(by_section, config):
            section(_section_label(section_id), by_section[section_id], indent="    ")
            year_total.add(by_section[section_id])
        # Inject year-aggregate bean stats (deduped across banks) — the
        # add() loop above only sums CSV-side counts.
        year_agg = bean_stats.get(("", year))
        if year_agg is not None:
            year_total.total_in_bean = year_agg.total_in_bean
            year_total.bean_unmatched = year_agg.bean_unmatched
        section(f"TOTAL {year}", year_total, indent="    ", show_rollup=True)
        overall.add(year_total)
        overall.total_in_bean += year_total.total_in_bean
        overall.bean_unmatched += year_total.bean_unmatched

    if len(by_year) > 1:
        console.print("\n  [bold magenta]── OVERALL ──[/]")
        section("TOTAL", overall, indent="    ", show_rollup=True)

    manual = overall.update_manual + overall.import_manual
    auto = overall.update_auto + overall.import_auto
    console.print()
    if manual:
        console.print(f"  [cyan]→ {manual} transaction(s) need manual attention[/]")
    if auto:
        console.print(f"  [green]→ {auto} transaction(s) would auto-apply via rules[/]")


def _print_summary(
    results: list[ImportResult],
    config: Config,
    bean_stats: dict[tuple[str, int], BeanProvenanceStats],
    *,
    dry_run: bool,
) -> None:
    counts: dict[str, int] = {}
    for r in results:
        action = r.action
        if action == "update" and not r.proposed_changes:
            action = "skip"
        counts[action] = counts.get(action, 0) + 1
    rendered = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    prefix = "[yellow]dry-run[/] " if dry_run else ""
    console.print(f"\n{prefix}done: {rendered or 'no transactions'}")

    _print_touched_entries(results, config, dry_run=dry_run)

    # One hint per section with unmatched ledger entries. Same data the
    # preview surfaces, kept short for the interactive summary so users see
    # at a glance how much of the ledger has no CSV provenance.
    by_section_unmatched: dict[str, int] = {}
    for (section, _year), prov in bean_stats.items():
        if section == "" or not prov.bean_unmatched:
            continue
        by_section_unmatched[section] = (
            by_section_unmatched.get(section, 0) + prov.bean_unmatched
        )
    if by_section_unmatched:
        configured_order = [b.account for b in config.banks]
        ordered = sorted(
            by_section_unmatched,
            key=lambda s: (
                configured_order.index(s) if s in configured_order else len(configured_order),
                s,
            ),
        )
        for section in ordered:
            label = _section_label(section)
            n = by_section_unmatched[section]
            console.print(
                f"  [magenta]→ {n} {label} transaction(s) have no CSV source[/]"
            )


def _print_touched_entries(
    results: list[ImportResult],
    config: Config,
    *,
    dry_run: bool,
) -> None:
    """List the entries touched by this run, grouped by action.

    Updates show the source-bean location plus the field(s) that changed
    so the user has a paper trail without diffing the file. New entries
    show the destination file and the proposal's target account. Skip
    counts are aggregated by source (silent vs user) since per-row
    detail isn't useful there.

    Under `--dry-run` the section header marks the lines as a
    would-have-written preview.
    """
    updated = [
        r for r in results
        if r.action == "update" and r.proposed_changes and r.matched_entry
    ]
    new_entries = [r for r in results if r.action == "new" and r.new_entry_text]
    silent_skips = sum(
        1
        for r in results
        if (r.action == "skip" and r.skip_reason in (
            "duplicate", "skip_rule", "cross_source_match"
        ))
        or (r.action == "update" and not r.proposed_changes)
    )
    user_skips = sum(
        1
        for r in results
        if r.skip_reason in ("user_skipped", "user_kept", "user_blocked")
    )

    if not (updated or new_entries or silent_skips or user_skips):
        return

    if updated:
        header = "[yellow]Would update[/]" if dry_run else "[green]Updated[/]"
        console.print(f"\n{header}:")
        for r in updated:
            entry = r.matched_entry
            assert entry is not None
            path = Path(entry.file_path).name if entry.file_path else "<in-session>"
            fields = ", ".join(c.field for c in r.proposed_changes)
            payee = r.proposal.payee if r.proposal and r.proposal.payee else (entry.payee or "")
            amount = f"{entry.amount} {entry.currency}"
            console.print(
                f"  {path}:{entry.line_start}  {payee}  {amount}  [dim]({fields})[/]"
            )

    if new_entries:
        header = "[yellow]Would create[/]" if dry_run else "[green]New[/]"
        console.print(f"\n{header}:")
        for r in new_entries:
            try:
                bank_cfg = config.bank(r.source_txn.bank_key)
                out_path = _resolve(
                    Path("."), bank_cfg.output_file, r.source_txn.booking_date.year
                ).name
            except KeyError:
                out_path = "<unknown>"
            target = r.proposal.target_account if r.proposal else ""
            amount = f"{r.source_txn.amount} {r.source_txn.currency}"
            payee = r.proposal.payee if r.proposal and r.proposal.payee else (r.source_txn.payee or "")
            console.print(
                f"  {out_path}  {payee}  {amount}  [dim]→ {target}[/]"
            )

    if silent_skips or user_skips:
        parts = []
        if silent_skips:
            parts.append(f"silent: {silent_skips}")
        if user_skips:
            parts.append(f"user: {user_skips}")
        console.print(f"\n[dim]Skipped — {', '.join(parts)}[/]")


_STARTER_CONFIG = """\
# Starter configuration for beancount-importer.
# All paths in this file are resolved relative to the *finances root* —
# the parent of `.beancount-importer/` in the standard layout, or whatever
# you pass via `--root`. State files keep living inside the dotted folder.
# Add one [[banks]] section per bank. {year} is substituted at run time.

rules_file = ".beancount-importer/rules.json"
decisions_file = ".beancount-importer/decisions.jsonl"
tag_state_file = ".beancount-importer/tag_state.json"
documents_dir = "documents"
transactions_dir = "transactions"

[matching]
min_score = 0.35

# [[banks]]
# key = "spk"
# display_name = "Sparkasse"
# account = "Assets:B:SPK"
# file_glob = "documents/**/SPK_*.csv"
# output_file = "transactions/{year}/SPK.bean"
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


# ── Path resolution + preflight validation ───────────────────────────────────


def _resolve_finances_root(config_path: Path, override: Path | None) -> Path:
    """Pick the directory that relative paths in the TOML resolve against.

    Priority: explicit `--root` override > parent of `.beancount-importer/`
    when the config lives there > the config file's own parent directory.
    """
    if override is not None:
        return override.resolve()
    cfg = config_path.resolve()
    if cfg.parent.name == ".beancount-importer" and cfg.name == "config.toml":
        return cfg.parent.parent
    return cfg.parent


def _validate_paths(config: Config, config_path: Path, base_dir: Path) -> None:
    """Fail fast when required directories don't exist.

    On failure: print a colored "Resolved configuration" table with absolute
    paths and green/red markers, then exit. State files are dim — they're
    created on demand and not blocking. Per-bank file_globs report their
    absolute glob root and a match count (0 is dim, not red).
    """
    transactions_abs = (base_dir / config.transactions_dir).resolve()
    documents_abs = (base_dir / config.documents_dir).resolve()

    errors: list[str] = []
    if not transactions_abs.is_dir():
        errors.append(f"transactions_dir does not exist: {transactions_abs}")
    if not documents_abs.is_dir():
        errors.append(f"documents_dir does not exist: {documents_abs}")
    if not errors:
        return

    table = Table(title="Resolved configuration", show_header=False, expand=False)
    table.add_column("key", style="bold")
    table.add_column("value")

    def _row(label: str, path: Path, *, exists: bool, dim: bool = False) -> None:
        if dim:
            marker = "[dim]·[/]"
            value = f"[dim]{path}[/]"
        elif exists:
            marker = "[green]✓[/]"
            value = str(path)
        else:
            marker = "[red]✗[/]"
            value = f"[red]{path}[/]"
        table.add_row(label, f"{marker} {value}")

    table.add_row("config", str(config_path.resolve()))
    table.add_row("finances root", str(base_dir))
    _row("transactions_dir", transactions_abs, exists=transactions_abs.is_dir())
    _row("documents_dir", documents_abs, exists=documents_abs.is_dir())
    _row(
        "rules_file",
        (base_dir / config.rules_file).resolve(),
        exists=True,
        dim=True,
    )
    _row(
        "decisions_file",
        (base_dir / config.decisions_file).resolve(),
        exists=True,
        dim=True,
    )
    _row(
        "tag_state_file",
        (base_dir / config.tag_state_file).resolve(),
        exists=True,
        dim=True,
    )
    for bank in config.banks:
        glob_matches = list(base_dir.glob(bank.file_glob, case_sensitive=False))
        n = len(glob_matches)
        marker = "[dim]·[/]" if n == 0 else "[green]✓[/]"
        table.add_row(
            f"banks.{bank.key}.file_glob",
            f"{marker} {bank.file_glob}  [dim]({n} match{'es' if n != 1 else ''})[/]",
        )
        table.add_row(
            f"banks.{bank.key}.output_file",
            f"[dim]·[/] [dim]{(base_dir / bank.output_file).resolve()}[/]",
        )

    console.print(table)
    for err in errors:
        console.print(f"[red]error:[/] {err}")
    raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
