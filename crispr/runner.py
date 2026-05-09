"""Runner that executes mutations — sequential or parallel with worker dirs.

Parallel mode:
  1. Creates ``.crispr/worker_0/`` … ``.crispr/worker_N-1/`` (shallow copies)
  2. Each worker picks a mutation, patches its own copy, runs the targeted
     test command, restores the file.
  3. Results stream back via a thread-safe callback.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .engine import Mutation, MutationResult, apply_mutation


WORKER_DIR = ".crispr"

# Directories never copied into workers
_COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".git", ".crispr", ".crispr-cache.db*",
    ".tox", ".venv", "venv", "env", "node_modules",
    ".mypy_cache", ".pytest_cache", ".eggs",
    "*.egg-info", ".coverage", "htmlcov",
)


@dataclass
class RunConfig:
    """Configuration for a mutation testing run."""

    project_root: Path
    test_command: list[str]
    timeout: float = 30.0
    workers: int = 1
    show_diff: bool = False


# ═══════════════════════════════════════════════════════════════════════════
# Worker directory management
# ═══════════════════════════════════════════════════════════════════════════

def setup_workers(project_root: Path, n: int) -> list[Path]:
    """Create N worker copies of the project in .crispr/worker_N/."""
    base = project_root / WORKER_DIR
    base.mkdir(exist_ok=True)

    workers: list[Path] = []
    for i in range(1, n + 1):
        worker_dir = base / f"worker_{i}"
        if worker_dir.exists():
            shutil.rmtree(worker_dir)
        shutil.copytree(
            project_root, worker_dir,
            ignore=_COPY_IGNORE,
            symlinks=True,
            dirs_exist_ok=False,
        )
        workers.append(worker_dir)

    return workers


def cleanup_workers(project_root: Path) -> None:
    """Remove all worker directories."""
    base = project_root / WORKER_DIR
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# Single mutation execution (used by both sequential and parallel)
# ═══════════════════════════════════════════════════════════════════════════

def _exec_mutation(
    worker_dir: str,
    filepath: str,
    source: str,
    mutation_index: int,
    test_command: list[str],
    timeout: float,
    operator_names: list[str] | None = None,
    skip_lines: set[int] | None = None,
    pragma_skips: dict | None = None,
) -> dict:
    """Execute one mutation in a worker directory.

    Called in a subprocess (parallel) or directly (sequential).
    Returns a plain dict (picklable across process boundaries).

    The worker must regenerate the mutation list using the *same* filters
    the CLI applied (per-file operator allow-list, coverage skip lines,
    pragma skips) — otherwise ``mutation_index`` lands on a different
    mutation and we report mismatched metadata vs. the change actually run.
    """
    from .engine import generate_mutations, apply_mutation
    from .diff import mutation_diff
    from .operators import get_operators

    ops = get_operators(operator_names) if operator_names is not None else None
    mutations = generate_mutations(
        source, filepath,
        operators=ops,
        skip_lines=skip_lines,
        pragma_skips=pragma_skips,
    )
    if mutation_index >= len(mutations):
        return {
            "index": mutation_index,
            "status": "error",
            "duration_s": 0.0,
            "output": "Mutation index out of range",
            "diff": "",
        }

    mutation = mutations[mutation_index]
    target_path = Path(worker_dir) / filepath

    try:
        mutated_source = apply_mutation(source, filepath, mutation)
    except Exception as exc:
        return {
            "index": mutation_index,
            "status": "error",
            "duration_s": 0.0,
            "output": f"Apply error: {exc}",
            "diff": "",
        }

    backup = target_path.read_text(encoding="utf-8")
    try:
        target_path.write_text(mutated_source, encoding="utf-8")
        start = time.monotonic()
        proc = subprocess.run(
            test_command,
            cwd=worker_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        elapsed = time.monotonic() - start

        status = "killed" if proc.returncode != 0 else "survived"
        diff_text = ""
        if status == "survived":
            diff_text = mutation_diff(source, filepath, mutation)

        return {
            "index": mutation_index,
            "status": status,
            "duration_s": elapsed,
            "output": (proc.stdout + proc.stderr)[-2000:],
            "diff": diff_text,
        }

    except subprocess.TimeoutExpired:
        return {
            "index": mutation_index,
            "status": "timeout",
            "duration_s": timeout,
            "output": "Test suite timed out",
            "diff": "",
        }
    except Exception as exc:
        return {
            "index": mutation_index,
            "status": "error",
            "duration_s": 0.0,
            "output": str(exc),
            "diff": "",
        }
    finally:
        target_path.write_text(backup, encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# Sequential runner (workers=1, uses project root directly)
# ═══════════════════════════════════════════════════════════════════════════

def run_mutations_sequential(
    config: RunConfig,
    filepath: str,
    source: str,
    mutations: list[Mutation],
    mutation_indices: list[int],
    test_commands: list[list[str]],
    progress_callback: Callable[[int, int, MutationResult], None] | None = None,
    worker_dir: Path | None = None,
    operator_names: list[str] | None = None,
    skip_lines: set[int] | None = None,
    pragma_skips: dict | None = None,
) -> list[MutationResult]:
    """Run mutations one at a time in a worker directory."""
    results: list[MutationResult] = []
    work_dir = str(worker_dir) if worker_dir else str(config.project_root)

    for seq, (mutation, idx, cmd) in enumerate(zip(mutations, mutation_indices, test_commands)):
        result_dict = _exec_mutation(
            work_dir, filepath, source, idx, cmd, config.timeout,
            operator_names, skip_lines, pragma_skips,
        )
        r = MutationResult(
            mutation=mutation,
            status=result_dict["status"],
            duration_s=result_dict["duration_s"],
            output=result_dict["output"],
            diff=result_dict.get("diff", ""),
        )
        results.append(r)
        if progress_callback:
            progress_callback(seq + 1, len(mutations), r)

    return results


# ═══════════════════════════════════════════════════════════════════════════
# Parallel runner (workers>1, uses .crispr/worker_N/)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class _MutationJob:
    """All info needed to run one mutation in a worker."""
    mutation: Mutation
    mutation_index: int
    filepath: str
    source: str
    test_command: list[str]
    operator_names: list[str] | None = None
    skip_lines: set[int] | None = None
    pragma_skips: dict | None = None


def run_mutations_parallel(
    config: RunConfig,
    jobs: list[_MutationJob],
    worker_dirs: list[Path],
    progress_callback: Callable[[int, int, MutationResult], None] | None = None,
) -> list[MutationResult]:
    """Run mutations across multiple worker directories in parallel."""
    n_workers = len(worker_dirs)
    results: list[MutationResult | None] = [None] * len(jobs)
    completed = 0
    total = len(jobs)

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        # Submit all jobs, cycling through workers
        future_to_idx = {}
        for i, job in enumerate(jobs):
            worker = str(worker_dirs[i % n_workers])
            future = executor.submit(
                _exec_mutation,
                worker,
                job.filepath,
                job.source,
                job.mutation_index,
                job.test_command,
                config.timeout,
                job.operator_names,
                job.skip_lines,
                job.pragma_skips,
            )
            future_to_idx[future] = i

        # Collect results as they complete
        for future in as_completed(future_to_idx):
            i = future_to_idx[future]
            job = jobs[i]
            try:
                result_dict = future.result()
            except Exception as exc:
                result_dict = {
                    "index": job.mutation_index,
                    "status": "error",
                    "duration_s": 0.0,
                    "output": f"Worker error: {exc}",
                    "diff": "",
                }

            r = MutationResult(
                mutation=job.mutation,
                status=result_dict["status"],
                duration_s=result_dict["duration_s"],
                output=result_dict["output"],
                diff=result_dict.get("diff", ""),
            )
            results[i] = r
            completed += 1

            if progress_callback:
                progress_callback(completed, total, r)

    return [r for r in results if r is not None]


# ═══════════════════════════════════════════════════════════════════════════
# Baseline check
# ═══════════════════════════════════════════════════════════════════════════

def check_baseline(config: RunConfig) -> tuple[bool, str]:
    """Run the test suite once to make sure it passes."""
    try:
        result = subprocess.run(
            config.test_command,
            cwd=str(config.project_root),
            capture_output=True,
            text=True,
            timeout=config.timeout * 3,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        passed = result.returncode == 0
        output = result.stdout + result.stderr
        return passed, output[-3000:]
    except subprocess.TimeoutExpired:
        return False, "Baseline test run timed out"
    except Exception as exc:
        return False, f"Error running baseline: {exc}"
