"""CLI for crispr — typer + tqdm."""

from __future__ import annotations

import hashlib
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from tqdm import tqdm

from . import __version__
from .cache import (
    MutationCache,
    discover_test_files,
    hash_source,
    hash_tests,
    mutation_id,
)
from .config import (
    CrisprConfig,
    get_source_line,
    is_line_ignored,
    load_config,
    merge_cli_over_config,
    operators_for_file,
)
from .coverage_filter import CoverageData, build_targeted_command, run_coverage_baseline
from .diff import colorize_diff, mutation_diff
from .engine import (
    Mutation,
    MutationResult,
    apply_mutation,
    discover_files,
    generate_mutations,
    parse_pragma_skips,
)
from .operators import ALL_OPERATORS, get_operators
from .reporter import (
    Summary,
    print_summary,
    write_html_report,
    write_json_report,
    write_junit_report,
)
from .runner import (
    RunConfig,
    _MutationJob,
    check_baseline,
    cleanup_workers,
    run_mutations_parallel,
    run_mutations_sequential,
    setup_workers,
)

app = typer.Typer(
    name="crispr",
    help="Fast AST-based mutation testing for Python.",
    add_completion=False,
    invoke_without_command=True,
)

BACKUP_DIR = ".crispr/backup"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"crispr {__version__}")
        raise typer.Exit()


_OPERATOR_CATEGORIES: list[tuple[str, list[tuple[str, str]]]] = [
    ("Arithmetic", [
        ("arithmetic",          "a + b → a - b"),
        ("bitwise",             "a & b → a | b"),
        ("aug_assign",          "x += 1 → x -= 1"),
        ("range_boundary",      "range(10) → range(11)"),
    ]),
    ("Logic", [
        ("comparison",          "x == y → x != y"),
        ("comparison_boundary", "x < y → x <= y"),
        ("boolean",             "a and b → a or b"),
        ("negate_condition",    "if x: → if not x:"),
        ("unary",               "-x → +x, not x → x"),
    ]),
    ("Values", [
        ("constant",            "42 → 43, True → False"),
        ("string_mutation",     "\"foo\" → \"XXfooXX\" / lower / upper"),
        ("fstring",             "f\"x={x}\" → f\"{x}\""),
        ("default_param",       "def f(x=10) → def f(x=None)"),
    ]),
    ("Control", [
        ("return",              "return x → return None"),
        ("yield",               "yield x → yield None"),
        ("if_else_swap",        "swaps the bodies of if/else"),
        ("ternary_swap",        "a if c else b → b if c else a"),
        ("break_continue",      "break ↔ continue"),
        ("remove_await",        "await f() → f()"),
    ]),
    ("Statements", [
        ("stmt_deletion",       "any statement → pass"),
        ("assign_to_none",      "x = expr → x = None"),
        ("assert_removal",      "assert cond → pass"),
        ("decorator_removal",   "@cache\\ndef f(): ... → def f(): ..."),
    ]),
    ("Calls", [
        ("call_arg",            "drop / swap arguments"),
        ("keyword_name",        "f(timeout=…) → f(retries=…)"),
        ("string_method_swap",  "\"x\".upper() → \"x\".lower()"),
        ("dict_method_swap",    "d.get(k) → d.pop(k)"),
        ("subscript",           "xs[0] → xs[1]"),
    ]),
    ("Errors", [
        ("exception_handler",   "except E: … → except E: pass"),
        ("exception_widen",     "except ValueError → except Exception"),
    ]),
    ("Misc", [
        ("comp_filter",         "[x for x in xs if cond] drops the if"),
    ]),
]


def _operators_list_callback(value: bool) -> None:
    if not value:
        return
    registered = {op.name for op in ALL_OPERATORS}
    documented: set[str] = set()
    name_w = max(len(name) for _, items in _OPERATOR_CATEGORIES for name, _ in items)
    _BOLD, _DIM, _RST = "\033[1m", "\033[2m", "\033[0m"

    typer.echo(f"\n  {_BOLD}Available mutation operators ({len(registered)}){_RST}\n")
    for category, items in _OPERATOR_CATEGORIES:
        typer.echo(f"  {_BOLD}{category}{_RST}")
        for name, example in items:
            documented.add(name)
            typer.echo(f"      {name:<{name_w}}  {_DIM}{example}{_RST}")
        typer.echo()

    missing = sorted(registered - documented)
    if missing:
        typer.echo(f"  {_BOLD}Other{_RST}")
        for name in missing:
            typer.echo(f"      {name}")
        typer.echo()

    typer.echo(f"  {_DIM}Use -o/--operators NAME ... to restrict a run to a subset.{_RST}\n")
    raise typer.Exit()


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", callback=_version_callback, is_eager=True),
    operators_list: bool = typer.Option(
        False, "--operators-list",
        callback=_operators_list_callback, is_eager=True,
        help="List available mutation operators and exit.",
    ),
) -> None:
    """Fast AST-based mutation testing for Python."""
    if ctx.invoked_subcommand is None:
        # No subcommand → show help
        typer.echo(ctx.get_help())
        raise typer.Exit()


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════

def _header(cfg: CrisprConfig, root: Path) -> None:
    sep = "\u2501" * 60
    typer.echo(f"\n{sep}")
    typer.echo(f"  crispr {__version__} \u2014 mutation testing for Python")
    typer.echo(sep)
    typer.echo(f"  Root:      {root}")
    typer.echo(f"  Command:   {cfg.command}")
    typer.echo(f"  Timeout:   {cfg.timeout}s")
    typer.echo(f"  Cache:     {'off' if cfg.no_cache else 'on'}")
    typer.echo(f"  Coverage:  {'on' if cfg.coverage else 'off'}")
    typer.echo(f"  Parallel:  {cfg.workers} worker{'s' if cfg.workers > 1 else ''}")
    if cfg.rules:
        typer.echo(f"  Rules:     {len(cfg.rules)} glob rule(s)")
    if cfg.ignore_patterns:
        typer.echo(f"  Ignore:    {len(cfg.ignore_patterns)} pattern(s)")
    typer.echo()


def _load_cfg(path: str, config: Optional[str], **cli_overrides) -> tuple[Path, CrisprConfig]:
    root = Path(path).resolve()
    config_path = Path(config) if config else None
    cfg = load_config(config_path=config_path, project_root=root)
    cfg = merge_cli_over_config(cfg, cli_overrides)
    cfg.workers = max(1, cfg.workers)
    return root, cfg


# ══════════════════════════════════════════════════════════════
# run (default command)
# ══════════════════════════════════════════════════════════════

@app.command()
def run(
    path: str = typer.Argument(".", help="Project root"),
    command: Optional[str] = typer.Option(None, "-c", "--command", help="Test command"),
    workers: Optional[int] = typer.Option(None, "-j", "--workers", help="Parallel workers"),
    include: Optional[list[str]] = typer.Option(None, "-i", "--include", help="Include patterns"),
    exclude: Optional[list[str]] = typer.Option(None, "-e", "--exclude", help="Exclude dirs"),
    operators: Optional[list[str]] = typer.Option(None, "-o", "--operators", help="Operators"),
    timeout: Optional[float] = typer.Option(None, "-t", "--timeout", help="Timeout (s)"),
    config: Optional[str] = typer.Option(None, "--config", help="Config file path"),
    no_baseline: bool = typer.Option(False, "--no-baseline", help="Skip baseline"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Disable cache"),
    coverage: bool = typer.Option(False, "--coverage", help="Use coverage.py"),
    source_dirs: Optional[list[str]] = typer.Option(None, "--source-dirs", help="Coverage source dirs"),
    json: Optional[str] = typer.Option(None, "--json", help="JSON report path"),
    html: Optional[str] = typer.Option(None, "--html", help="HTML report path"),
    junit: Optional[str] = typer.Option(None, "--junit", help="JUnit XML report path"),
    quiet: bool = typer.Option(False, "-q", "--quiet", help="Summary only"),
    debug: bool = typer.Option(False, "--debug", help="List mutations per file before running"),
    dry_run: bool = typer.Option(False, "--dry-run", help="List mutations only"),
) -> None:
    """Run mutation testing."""

    root, cfg = _load_cfg(
        path, config, command=command, workers=workers, include=include,
        exclude=exclude, operators=operators, timeout=timeout,
        no_baseline=no_baseline, no_cache=no_cache, coverage=coverage,
        source_dirs=source_dirs, json=json, html=html, junit=junit,
        quiet=quiet, debug=debug, dry_run=dry_run,
    )

    try:
        test_cmd = cfg.command.split()
        all_op_names = [op.name for op in ALL_OPERATORS]
        global_operators = get_operators(cfg.operators)
        run_cfg = RunConfig(project_root=root, test_command=test_cmd, timeout=cfg.timeout, workers=cfg.workers)

        _header(cfg, root)

        # --- Cache ---
        cache: MutationCache | None = None
        tests_sha = ""
        if not cfg.no_cache:
            cache = MutationCache(root)
            test_files = discover_test_files(root)
            tests_sha = hash_tests(test_files) if test_files else ""
            old = cache.get_tests_sha()
            if old is not None and old != tests_sha:
                typer.echo("  Tests changed \u2014 full re-run required\n")
            cache.set_tests_sha(tests_sha)

        # --- Discover ---
        files = discover_files(root, include=cfg.include, exclude=cfg.exclude)
        if not files:
            typer.echo("  No Python files found.")
            raise typer.Exit(1)
        typer.echo(f"  Found {len(files)} source file(s)\n")

        # --- Baseline ---
        cov_data: CoverageData | None = None
        if cfg.coverage:
            cov_key = ""
            if cache:
                kh = hashlib.sha256()
                kh.update(tests_sha.encode())
                kh.update(hash_tests(files).encode())
                kh.update(" ".join(test_cmd).encode())
                kh.update(",".join(sorted(cfg.source_dirs or [])).encode())
                cov_key = kh.hexdigest()
                cached = cache.get_coverage_payload(cov_key)
                if cached is not None:
                    try:
                        cov_data = CoverageData.from_json(cached)
                    except (KeyError, ValueError):
                        cov_data = None
            if cov_data is not None:
                total_cov = sum(len(v) for v in cov_data.covered_lines.values())
                ctx_n = sum(len(v) for v in cov_data.line_contexts.values()) if cov_data.has_contexts else 0
                ctx_note = f", {ctx_n} with test mapping" if ctx_n else ""
                typer.echo(f"  Reusing cached coverage baseline ({total_cov} lines covered{ctx_note})\n")
            else:
                typer.echo("  Running baseline with coverage (dynamic contexts)...", nl=False)
                cov_data = run_coverage_baseline(root, test_cmd, cfg.timeout, source_dirs=cfg.source_dirs)
                if not cov_data.passed:
                    typer.echo(f" FAILED\n\n{cov_data.output}")
                    raise typer.Exit(1)
                total_cov = sum(len(v) for v in cov_data.covered_lines.values())
                ctx_n = sum(len(v) for v in cov_data.line_contexts.values()) if cov_data.has_contexts else 0
                ctx_note = f", {ctx_n} with test mapping" if ctx_n else ""
                typer.echo(f" OK ({total_cov} lines covered{ctx_note})\n")
                if cache and cov_key:
                    cache.set_coverage_payload(cov_key, cov_data.to_json())
        elif not cfg.no_baseline:
            typer.echo("  Running baseline tests...", nl=False)
            passed, output = check_baseline(run_cfg)
            if not passed:
                typer.echo(f" FAILED\n\n{output}")
                raise typer.Exit(1)
            typer.echo(" OK\n")

        # --- Generate mutations ---
        FileEntry = tuple[str, str, list[Mutation], str]
        file_entries: list[FileEntry] = []
        sources: dict[str, str] = {}  # rel → source (for ignore_patterns)

        for fpath in files:
            rel = str(fpath.relative_to(root))
            source = fpath.read_text(encoding="utf-8")
            sources[rel] = source
            src_sha = hash_source(fpath) if cache else ""
            pragma_skips = parse_pragma_skips(source)
            skip_lines: set[int] = set()

            if cfg.coverage and cov_data:
                if rel in cov_data.covered_lines:
                    all_lines = set(range(1, source.count("\n") + 2))
                    skip_lines = all_lines - cov_data.covered_lines[rel]
                else:
                    continue

            if cfg.rules:
                file_operators = get_operators(operators_for_file(rel, all_op_names, cfg.rules))
            else:
                file_operators = global_operators

            mutations = generate_mutations(
                source, rel, operators=file_operators,
                skip_lines=skip_lines, pragma_skips=pragma_skips,
            )
            if mutations:
                file_entries.append((rel, source, mutations, src_sha))

        total_mutations = sum(len(e[2]) for e in file_entries)
        typer.echo(f"  Generated {total_mutations} mutation(s)\n")

        if cfg.debug:
            for rel, _, mutations, _ in file_entries:
                tags = "/".join(f"{m.operator}@{m.lineno}" for m in mutations)
                typer.echo(f"  {rel} : {tags}")
            typer.echo()
            op_counts: dict[str, int] = {}
            for _, _, mutations, _ in file_entries:
                for m in mutations:
                    op_counts[m.operator] = op_counts.get(m.operator, 0) + 1
            if op_counts:
                typer.echo(f"  Mutants by operator ({total_mutations} total):")
                width = max(len(op) for op in op_counts)
                for op, n in sorted(op_counts.items(), key=lambda kv: (-kv[1], kv[0])):
                    pct = n * 100 / total_mutations
                    typer.echo(f"    {op:<{width}}  {n:>5}  {pct:>5.1f}%")
                typer.echo()

            actual_lines = sum(len({m.lineno for m in muts}) for _, _, muts, _ in file_entries)
            actual_files = len(file_entries)
            total_files = len(files)
            covered_lines = sum(len(v) for v in cov_data.covered_lines.values()) if cov_data else None
            covered_files = len(cov_data.covered_lines) if cov_data else None
            potential_lines = actual_lines
            potential_files = actual_files
            if cfg.rules:
                potential_lines = 0
                potential_files = 0
                for fpath in files:
                    rel = str(fpath.relative_to(root))
                    src = sources.get(rel)
                    if src is None:
                        continue
                    pskips = parse_pragma_skips(src)
                    sk: set[int] = set()
                    if cfg.coverage and cov_data:
                        if rel not in cov_data.covered_lines:
                            continue
                        all_ln = set(range(1, src.count("\n") + 2))
                        sk = all_ln - cov_data.covered_lines[rel]
                    pmuts = generate_mutations(
                        src, rel, operators=global_operators,
                        skip_lines=sk, pragma_skips=pskips,
                    )
                    if pmuts:
                        potential_files += 1
                        potential_lines += len({m.lineno for m in pmuts})
            typer.echo(f"  Source files:                  {total_files}")
            if covered_files is not None:
                typer.echo(f"  Covered files (baseline):      {covered_files}")
            if cfg.rules:
                typer.echo(f"  Mutable files (without rules): {potential_files}")
            typer.echo(f"  Mutable files (after rules):   {actual_files}")
            if covered_lines is not None:
                typer.echo(f"  Covered lines (baseline):      {covered_lines}")
            if cfg.rules:
                typer.echo(f"  Mutable lines (without rules): {potential_lines}")
            typer.echo(f"  Mutable lines (after rules):   {actual_lines}")
            typer.echo()

        if total_mutations == 0:
            typer.echo("  Nothing to mutate.")
            raise typer.Exit(0)

        if cfg.dry_run:
            for rel, _, mutations, _ in file_entries:
                for m in mutations:
                    mid = mutation_id(rel, m.operator, m.lineno, m.col_offset, m.description)
                    typer.echo(f"  {mid}  {m}")
            typer.echo(f"\n  Total: {total_mutations} mutations (dry run)")
            raise typer.Exit(0)

        # --- Build jobs ---
        all_jobs: list[tuple[str, str, str, Mutation, int, list[str]]] = []
        targeted_count = 0
        for rel, source, mutations, src_sha in file_entries:
            for idx, m in enumerate(mutations):
                mid = mutation_id(rel, m.operator, m.lineno, m.col_offset, m.description)
                if cache and cache.is_fresh(mid, src_sha, tests_sha):
                    continue
                cmd = test_cmd
                if cov_data and cov_data.has_contexts:
                    tests = cov_data.tests_for_mutation(rel, m.lineno)
                    if tests:
                        cmd = build_targeted_command(test_cmd, tests)
                        targeted_count += 1
                all_jobs.append((rel, source, src_sha, m, idx, cmd))

        # --- Cached results ---
        all_results: list[MutationResult] = []
        cached_count = 0
        for rel, source, mutations, src_sha in file_entries:
            for m in mutations:
                mid = mutation_id(rel, m.operator, m.lineno, m.col_offset, m.description)
                if cache and cache.is_fresh(mid, src_sha, tests_sha):
                    cr = cache.get_result(mid)
                    if cr and cr.status:
                        diff_text = mutation_diff(source, rel, m) if cr.status == "survived" else ""
                        all_results.append(MutationResult(
                            mutation=m, status=cr.status,
                            duration_s=cr.duration_s, output=cr.output, diff=diff_text,
                        ))
                        cached_count += 1

        if cached_count and not cfg.quiet:
            typer.echo(f"  \033[36mCache: {cached_count} results reused\033[0m")
        if targeted_count and not cfg.quiet:
            typer.echo(f"  \033[36mTargeted tests: {targeted_count} mutations\033[0m")

        if not all_jobs:
            typer.echo(f"  All {total_mutations} mutations cached.\n")
        else:
            typer.echo(f"\n  Running {len(all_jobs)} mutation(s)...\n")

        # --- Execute with tqdm ---
        start_time = time.monotonic()
        new_results: list[MutationResult] = []

        if all_jobs:
            actual_workers = min(cfg.workers, len(all_jobs))
            typer.echo(f"  Setting up {actual_workers} worker(s)...", nl=False)
            worker_dirs = setup_workers(root, actual_workers)
            typer.echo(" OK\n")

            jobs = [
                _MutationJob(mutation=m, mutation_index=idx, filepath=rel,
                             source=source, test_command=cmd)
                for rel, source, _, m, idx, cmd in all_jobs
            ]

            bar = tqdm(
                total=len(jobs), desc="  Mutating",
                unit="mut", leave=True, disable=cfg.quiet,
                bar_format="  {l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}{postfix}]",
            )

            counts = {"killed": 0, "survived": 0, "timeout": 0, "error": 0}
            _G, _R, _Y, _M, _X = "\033[32m", "\033[31m", "\033[33m", "\033[35m", "\033[0m"

            def _on_progress(cur: int, tot: int, r: MutationResult) -> None:
                counts[r.status] = counts.get(r.status, 0) + 1
                bar.set_postfix_str(
                    f"{_G}killed={counts['killed']}{_X} "
                    f"{_R}survived={counts['survived']}{_X} "
                    f"{_Y}timeout={counts['timeout']}{_X} "
                    f"{_M}error={counts['error']}{_X}",
                    refresh=False,
                )
                bar.update(1)

            if cfg.workers > 1:
                new_results = run_mutations_parallel(run_cfg, jobs, worker_dirs, _on_progress)
            else:
                by_file: dict[str, list[tuple[Mutation, int, list[str]]]] = {}
                for rel, source, _, m, idx, cmd in all_jobs:
                    by_file.setdefault(rel, []).append((m, idx, cmd))

                for rel_key, entries in by_file.items():
                    src = next(s for r, s, _, _, _, _ in all_jobs if r == rel_key)
                    new_results.extend(run_mutations_sequential(
                        run_cfg, rel_key, src,
                        [e[0] for e in entries], [e[1] for e in entries], [e[2] for e in entries],
                        progress_callback=_on_progress, worker_dir=worker_dirs[0],
                    ))

            bar.close()
            typer.echo(f"  Cleaning up workers...", nl=False)
            cleanup_workers(root)
            typer.echo(" OK")

        # --- Store in cache ---
        if cache and new_results:
            for (rel, _, src_sha, m, _, _), r in zip(all_jobs, new_results):
                mid = mutation_id(rel, m.operator, m.lineno, m.col_offset, m.description)
                cache.store_result(
                    mid=mid, filepath=rel, lineno=m.lineno, col_offset=m.col_offset,
                    operator=m.operator, description=m.description,
                    status=r.status, duration_s=r.duration_s, output=r.output,
                    source_sha=src_sha, tests_sha=tests_sha,
                )
            for rel, _, src_sha, _, _, _ in {j[0]: j for j in all_jobs}.values():
                cache.set_source_sha(rel, src_sha)

        all_results.extend(new_results)
        elapsed = time.monotonic() - start_time

        # --- Apply ignore_patterns to survivors ---
        if cfg.ignore_patterns:
            ignored_count = 0
            for r in all_results:
                if r.status == "survived":
                    line = get_source_line(sources.get(r.mutation.file, ""), r.mutation.lineno)
                    if is_line_ignored(line, cfg.ignore_patterns):
                        # Replace status (MutationResult is frozen, create new)
                        idx = all_results.index(r)
                        all_results[idx] = MutationResult(
                            mutation=r.mutation, status="ignored",
                            duration_s=r.duration_s, output=r.output, diff=r.diff,
                        )
                        ignored_count += 1
            if ignored_count:
                typer.echo(f"\n  \033[2mIgnored: {ignored_count} survivor(s) matched ignore_patterns\033[0m")

        # --- Summary ---
        summary = Summary.from_results(all_results)
        summary.duration_s = elapsed
        print_summary(summary)

        if cfg.json_report:
            write_json_report(summary, Path(cfg.json_report))
        if cfg.html_report:
            write_html_report(summary, Path(cfg.html_report))
        if cfg.junit_report:
            write_junit_report(summary, Path(cfg.junit_report))
        if cache:
            cache.close()

        raise typer.Exit(0 if summary.survived == 0 else 1)
    finally:
        shutil.rmtree(root / ".crispr", ignore_errors=True)


# ══════════════════════════════════════════════════════════════
# show
# ══════════════════════════════════════════════════════════════

@app.command()
def show(
    mutation_id_arg: str = typer.Argument(..., metavar="ID", help="Mutation ID (12-char hex)"),
    path: str = typer.Argument(".", help="Project root"),
) -> None:
    """Show diff for a mutation by ID."""
    root = Path(path).resolve()
    cache = MutationCache(root)
    result = cache.get_result(mutation_id_arg)
    cache.close()

    if result is None:
        typer.echo(f"  No mutation found with ID {mutation_id_arg}")
        raise typer.Exit(1)

    typer.echo(f"\n  Mutation  {result.mutation_id}")
    typer.echo(f"  File:     {result.filepath}:{result.lineno}")
    typer.echo(f"  Operator: {result.operator}")
    typer.echo(f"  Desc:     {result.description}")
    typer.echo(f"  Status:   {result.status}\n")

    fpath = root / result.filepath
    if not fpath.exists():
        typer.echo(f"  (file not found)")
        return

    source = fpath.read_text(encoding="utf-8")
    mutations = generate_mutations(source, result.filepath)
    for m in mutations:
        mid = mutation_id(result.filepath, m.operator, m.lineno, m.col_offset, m.description)
        if mid == result.mutation_id:
            diff_text = mutation_diff(source, result.filepath, m)
            typer.echo(colorize_diff(diff_text) if diff_text else "  (no diff)")
            return

    typer.echo("  (source changed \u2014 cannot regenerate diff)")


# ══════════════════════════════════════════════════════════════
# apply / revert
# ══════════════════════════════════════════════════════════════

@app.command()
def apply(
    mutation_id_arg: str = typer.Argument(..., metavar="ID", help="Mutation ID to apply"),
    path: str = typer.Argument(".", help="Project root"),
    revert: bool = typer.Option(False, "--revert", help="Revert applied mutation from backup"),
) -> None:
    """Apply a mutation to disk for debugging, or revert it."""
    root = Path(path).resolve()
    backup_base = root / BACKUP_DIR

    if revert:
        if not backup_base.exists():
            typer.echo("  No backup found — nothing to revert.")
            raise typer.Exit(1)
        restored = 0
        for backup_file in backup_base.rglob("*"):
            if backup_file.is_file():
                rel = backup_file.relative_to(backup_base)
                target = root / rel
                shutil.copy2(backup_file, target)
                typer.echo(f"  Restored {rel}")
                restored += 1
        shutil.rmtree(backup_base)
        typer.echo(f"\n  Reverted {restored} file(s). Backup cleared.")
        return

    # --- Apply mutation ---
    cache = MutationCache(root)
    result = cache.get_result(mutation_id_arg)
    cache.close()

    if result is None:
        typer.echo(f"  No mutation found with ID {mutation_id_arg}")
        raise typer.Exit(1)

    fpath = root / result.filepath
    if not fpath.exists():
        typer.echo(f"  File not found: {result.filepath}")
        raise typer.Exit(1)

    source = fpath.read_text(encoding="utf-8")

    # Find and apply mutation
    mutations = generate_mutations(source, result.filepath)
    target_m = None
    for m in mutations:
        mid = mutation_id(result.filepath, m.operator, m.lineno, m.col_offset, m.description)
        if mid == mutation_id_arg:
            target_m = m
            break

    if target_m is None:
        typer.echo("  Source changed — mutation no longer exists.")
        raise typer.Exit(1)

    mutated = apply_mutation(source, result.filepath, target_m)

    # Save backup
    backup_path = backup_base / result.filepath
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    if not backup_path.exists():
        shutil.copy2(fpath, backup_path)

    # Write mutated file
    fpath.write_text(mutated, encoding="utf-8")

    typer.echo(f"\n  Applied mutation {mutation_id_arg}")
    typer.echo(f"  File:   {result.filepath}:{result.lineno}")
    typer.echo(f"  Desc:   [{result.operator}] {result.description}")
    typer.echo(f"  Backup: {backup_path}")
    typer.echo(f"\n  Run \033[1mcrispr apply {mutation_id_arg} --revert\033[0m to restore.")


# ══════════════════════════════════════════════════════════════
# results
# ══════════════════════════════════════════════════════════════

@app.command()
def results(
    path: str = typer.Argument(".", help="Project root"),
    survivors: bool = typer.Option(False, "--survivors", help="Only survivors"),
    file: Optional[str] = typer.Option(None, "--file", help="Filter by file"),
) -> None:
    """Show cached mutation results."""
    root = Path(path).resolve()
    cache = MutationCache(root)
    if survivors:
        items = cache.survivors()
        label = "Surviving mutations"
    elif file:
        items = cache.all_results(filepath=file)
        label = f"Mutations for {file}"
    else:
        items = cache.all_results()
        label = "All cached mutations"
    cache.close()

    if not items:
        typer.echo("  No cached results.")
        return

    _SC = {"killed": "\033[32m", "survived": "\033[31m", "timeout": "\033[33m", "error": "\033[35m", "ignored": "\033[2m"}
    R = "\033[0m"
    typer.echo(f"\n  {label} ({len(items)}):\n")
    for r in items:
        c = _SC.get(r.status or "", "")
        typer.echo(f"  {r.mutation_id}  {c}{(r.status or 'pending'):>8}{R}  {r.filepath}:{r.lineno:<4}  [{r.operator}] {r.description}")
    typer.echo()


# ══════════════════════════════════════════════════════════════
# clear
# ══════════════════════════════════════════════════════════════

@app.command()
def clear(
    path: str = typer.Argument(".", help="Project root"),
) -> None:
    """Clear mutation cache and worker directories."""
    root = Path(path).resolve()
    cache = MutationCache(root)
    cache.clear()
    cache.close()
    cleanup_workers(root)
    backup = root / BACKUP_DIR
    if backup.exists():
        shutil.rmtree(backup)
    typer.echo("  Cache, workers, and backups cleared.")
