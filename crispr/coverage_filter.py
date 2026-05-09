"""Coverage.py integration with dynamic context support.

When ``--dynamic-context=test_function`` is used, coverage.py records which
test function executed each line.  We expose this as a mapping so the runner
can launch *only* the relevant tests for each mutation instead of the full
suite — the single biggest speed-up for mutation testing.

Two levels of data:

- ``covered_lines``:  {file: {line_numbers}}  — used to skip uncovered lines
- ``line_contexts``:  {file: {line: [test_ids]}} — used for targeted runs
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CoverageData:
    """Parsed coverage results."""

    passed: bool
    covered_lines: dict[str, set[int]]
    line_contexts: dict[str, dict[int, list[str]]]  # file → line → [test_ids]
    output: str

    @property
    def has_contexts(self) -> bool:
        return any(bool(v) for v in self.line_contexts.values())

    def tests_for_line(self, filepath: str, lineno: int) -> list[str] | None:
        """Return tests covering a specific line, or None if no context data."""
        file_ctx = self.line_contexts.get(filepath)
        if not file_ctx:
            return None
        return file_ctx.get(lineno)

    def to_json(self) -> str:
        """Serialize to JSON (sets → lists, int keys → strings)."""
        return json.dumps({
            "passed": self.passed,
            "covered_lines": {f: sorted(lines) for f, lines in self.covered_lines.items()},
            "line_contexts": {
                f: {str(ln): tests for ln, tests in ctx.items()}
                for f, ctx in self.line_contexts.items()
            },
            "output": self.output,
        })

    @classmethod
    def from_json(cls, payload: str) -> "CoverageData":
        d = json.loads(payload)
        return cls(
            passed=d["passed"],
            covered_lines={f: set(lines) for f, lines in d["covered_lines"].items()},
            line_contexts={
                f: {int(ln): tests for ln, tests in ctx.items()}
                for f, ctx in d["line_contexts"].items()
            },
            output=d["output"],
        )

    def tests_for_mutation(self, filepath: str, lineno: int) -> list[str] | None:
        """Return deduplicated test list for a mutation.

        Checks the exact line and ±2 neighbouring lines (a mutation on line N
        may be guarded by a branch on line N-1).
        """
        file_ctx = self.line_contexts.get(filepath)
        if not file_ctx:
            return None
        tests: set[str] = set()
        for ln in range(max(1, lineno - 2), lineno + 3):
            for t in file_ctx.get(ln, []):
                tests.add(t)
        return sorted(tests) if tests else None


def _is_pytest_command(test_command: list[str]) -> bool:
    """True if ``test_command`` invokes pytest."""
    if not test_command:
        return False
    if test_command[0] in ("pytest", "py.test"):
        return True
    # ``python -m pytest ...``
    if (
        len(test_command) >= 3
        and test_command[0] == "python"
        and test_command[1] == "-m"
        and test_command[2] in ("pytest", "py.test")
    ):
        return True
    return False


def _has_pytest_cov() -> bool:
    """Detect whether pytest-cov is importable in the current environment."""
    try:
        result = subprocess.run(
            ["python", "-c", "import pytest_cov"],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _build_pytest_cov_command(
    test_command: list[str],
    source_dirs: list[str] | None,
    cov_rc: Path,
) -> list[str]:
    """Build a ``pytest --cov ... --cov-context=test`` invocation.

    pytest-cov hooks into pytest's own setup/call/teardown events, which
    gives correct per-test attribution under async + importlib (unlike
    coverage.py's ``dynamic_context = test_function`` heuristic).
    """
    # Strip the leading ``python -m pytest`` / ``pytest`` from the user
    # command; we re-prepend our own form.
    if test_command[0] == "python" and len(test_command) >= 3 and test_command[1] == "-m":
        rest = test_command[3:]  # drop python, -m, pytest
    elif test_command[0] in ("pytest", "py.test"):
        rest = test_command[1:]
    else:
        rest = test_command  # shouldn't happen — guarded by _is_pytest_command

    cov_args: list[str] = []
    if source_dirs:
        for src in source_dirs:
            cov_args.append(f"--cov={src}")
    else:
        cov_args.append("--cov")
    cov_args += ["--cov-context=test", f"--cov-config={cov_rc}"]

    return ["python", "-m", "pytest"] + cov_args + rest


def _build_coverage_run_command(
    test_command: list[str],
    cov_rc: Path,
) -> list[str]:
    """Build a ``coverage run --rcfile=... -- <test command>`` invocation.

    Used as a fallback when the test runner isn't pytest, or pytest-cov
    isn't installed.
    """
    cov_cmd = ["python", "-m", "coverage", "run", f"--rcfile={cov_rc}"]
    if len(test_command) >= 3 and test_command[0] == "python" and test_command[1] == "-m":
        cov_cmd += ["-m"] + test_command[2:]
    elif len(test_command) >= 1 and test_command[0] in ("pytest", "py.test"):
        cov_cmd += ["-m"] + test_command
    else:
        cov_cmd += test_command
    return cov_cmd


def run_coverage_baseline(
    project_root: Path,
    test_command: list[str],
    timeout: float,
    source_dirs: list[str] | None = None,
) -> CoverageData:
    """Run the test suite under coverage with per-test contexts.

    For pytest test commands (when ``pytest-cov`` is installed), invokes
    ``pytest --cov --cov-context=test`` so pytest-cov's pytest-event hooks
    drive context attribution. This is required for correct per-test
    mapping under async tests + importlib, where coverage.py's own
    ``dynamic_context = test_function`` heuristic misattributes lines.

    For non-pytest runners (or when pytest-cov is missing), falls back to
    ``coverage run`` with ``dynamic_context = test_function``.
    """
    cov_json = Path(tempfile.mktemp(suffix=".json"))
    cov_data = cov_json.with_suffix(".coverage")
    cov_rc = cov_json.with_suffix(".coveragerc")

    use_pytest_cov = _is_pytest_command(test_command) and _has_pytest_cov()

    # Build .coveragerc — pytest-cov drives contexts via its own flag, so
    # we only set dynamic_context for the coverage-run fallback path.
    rc_content = "[run]\nbranch = true\n"
    if not use_pytest_cov:
        rc_content += "dynamic_context = test_function\n"
    if source_dirs:
        rc_content += f"source = {','.join(source_dirs)}\n"
    cov_rc.write_text(rc_content)

    if use_pytest_cov:
        cov_cmd = _build_pytest_cov_command(test_command, source_dirs, cov_rc)
    else:
        cov_cmd = _build_coverage_run_command(test_command, cov_rc)

    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "COVERAGE_FILE": str(cov_data),
    }

    # Run tests under coverage
    try:
        result = subprocess.run(
            cov_cmd, cwd=str(project_root),
            capture_output=True, text=True, timeout=timeout * 3, env=env,
        )
    except subprocess.TimeoutExpired:
        return CoverageData(False, {}, {}, "Coverage baseline timed out")
    except FileNotFoundError:
        return CoverageData(False, {}, {}, "coverage not found — pip install coverage")

    passed = result.returncode == 0
    output = (result.stdout + result.stderr)[-3000:]
    if not passed:
        return CoverageData(False, {}, {}, output)

    # Export to JSON (with contexts)
    try:
        export = subprocess.run(
            [
                "python", "-m", "coverage", "json",
                "-o", str(cov_json),
                "--show-contexts",
            ],
            cwd=str(project_root),
            capture_output=True, text=True, timeout=30, env=env,
        )
        if export.returncode != 0:
            return CoverageData(True, {}, {}, f"Tests passed but json export failed:\n{export.stderr}")
    except Exception as exc:
        return CoverageData(True, {}, {}, f"Tests passed but export error: {exc}")

    # Parse
    covered, contexts = _parse_coverage_json(cov_json, project_root)

    # Cleanup
    for f in [cov_json, cov_data, cov_rc]:
        f.unlink(missing_ok=True)

    return CoverageData(passed, covered, contexts, output)


def _parse_coverage_json(
    json_path: Path, project_root: Path,
) -> tuple[dict[str, set[int]], dict[str, dict[int, list[str]]]]:
    """Parse coverage JSON → (covered_lines, line_contexts)."""
    if not json_path.exists():
        return {}, {}

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}, {}

    covered: dict[str, set[int]] = {}
    contexts: dict[str, dict[int, list[str]]] = {}

    for abs_path, file_data in data.get("files", {}).items():
        try:
            rel = str(Path(abs_path).relative_to(project_root))
        except ValueError:
            rel = abs_path

        # Basic coverage
        executed = set(file_data.get("executed_lines", []))
        if executed:
            covered[rel] = executed

        # Context data: {"line_number": ["test_a|run", "test_b|run", ...]}
        raw_contexts = file_data.get("contexts", {})
        if raw_contexts:
            file_ctx: dict[int, list[str]] = {}
            for line_str, ctx_list in raw_contexts.items():
                try:
                    lineno = int(line_str)
                except ValueError:
                    continue
                # Clean context names: "test_file.py::test_func|run" → "test_file.py::test_func"
                tests = []
                for ctx in ctx_list:
                    if not ctx or ctx == "":
                        continue
                    # Strip "|run" suffix that coverage adds
                    clean = ctx.split("|")[0].strip()
                    if clean:
                        tests.append(clean)
                if tests:
                    file_ctx[lineno] = tests
            if file_ctx:
                contexts[rel] = file_ctx

    return covered, contexts


def build_targeted_command(
    base_command: list[str],
    test_ids: list[str],
) -> list[str]:
    """Build a pytest command that runs only the given test IDs.

    Coverage contexts use ``module.func`` format (e.g. ``test_calc.test_add``).
    Pytest expects ``file.py::func`` (e.g. ``test_calc.py::test_add``).
    """
    if not test_ids:
        return base_command

    # Only works with pytest
    cmd_str = " ".join(base_command)
    if "pytest" not in cmd_str and "py.test" not in cmd_str:
        return base_command

    # Extract only actual pytest flags, skip "python", "-m", "pytest", and
    # any positional test paths (don't start with -)
    skip = {"python", "pytest", "py.test"}
    flags: list[str] = []
    prev_was_m = False
    for arg in base_command:
        if prev_was_m:
            prev_was_m = False
            continue  # skip module name after -m
        if arg == "-m" and "pytest" not in flags:
            prev_was_m = True
            continue  # skip -m itself
        if arg in skip:
            continue
        if arg.startswith("-"):
            flags.append(arg)

    if "-x" not in flags:
        flags.append("-x")

    # Convert coverage test IDs to pytest node IDs:
    #   "test_calc.test_add" → "test_calc.py::test_add"
    #   "tests.test_budget.TestBudget.test_create" → "tests/test_budget.py::TestBudget::test_create"
    node_ids = [_context_to_pytest_id(tid) for tid in test_ids]
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_ids = []
    for nid in node_ids:
        if nid not in seen:
            seen.add(nid)
            unique_ids.append(nid)

    return ["python", "-m", "pytest"] + flags + unique_ids


def _context_to_pytest_id(ctx: str) -> str:
    """Convert a coverage context name to a pytest node ID.

    ``test_calc.test_add``  →  ``test_calc.py::test_add``
    ``tests.test_budget.TestBudget.test_create``  →  ``tests/test_budget.py::TestBudget::test_create``
    ``test_calc.py::test_add``  →  ``test_calc.py::test_add``  (already correct)
    """
    # Already a pytest node ID
    if "::" in ctx:
        return ctx

    parts = ctx.split(".")
    if len(parts) < 2:
        return ctx

    # Find the split point: the last part that looks like a module (starts with test_)
    # Everything up to and including the test module is the file path,
    # the rest is the test class/function chain
    file_parts: list[str] = []
    test_parts: list[str] = []
    found_module = False

    for i, part in enumerate(parts):
        if not found_module:
            file_parts.append(part)
            # A test module typically starts with test_ or ends with _test
            if part.startswith("test_") or part.endswith("_test"):
                found_module = True
        else:
            test_parts.append(part)

    if not found_module or not test_parts:
        # Fallback: assume last part is the function
        file_parts = parts[:-1]
        test_parts = parts[-1:]

    file_path = "/".join(file_parts[:-1]) + ("/" if len(file_parts) > 1 else "") + file_parts[-1] + ".py"
    # Clean up leading /
    file_path = file_path.lstrip("/")

    return file_path + "::" + "::".join(test_parts)
