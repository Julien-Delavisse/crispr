"""Configuration from ``pyproject.toml`` with CLI overrides.

Reads ``[tool.crispr]`` and ``[[tool.crispr.rules]]`` sections.

Example ``pyproject.toml``::

    [tool.crispr]
    command = "pytest tests/ -x -q --tb=no --no-header"
    timeout = 60
    workers = 4
    coverage = true
    include = ["src/"]
    exclude = [".venv", "migrations"]

    # Glob rules — last matching rule wins.
    # allowed_operators and excluded_operators are mutually exclusive.
    # allowed_operators = [] disables all mutations for matched files.

    [[tool.crispr.rules]]
    glob = "**/domain/**/*.py"
    allowed_operators = ["arithmetic", "comparison", "boolean"]

    [[tool.crispr.rules]]
    glob = "**/application/**/*.py"
    excluded_operators = ["string_mutation", "constant"]

    [[tool.crispr.rules]]
    glob = "**/generated/**/*.py"
    allowed_operators = []  # skip entirely
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pathspec


# ═══════════════════════════════════════════════════════════════════════════
# Glob rules
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class OperatorRule:
    """A glob pattern with an operator allow-list or deny-list.

    Patterns use **gitignore syntax** (via ``pathspec``): ``**`` matches
    zero or more path segments, ``*`` and ``?`` do not cross ``/``,
    ``!`` negates, a leading ``/`` anchors to the project root.

    ``allowed_operators`` and ``excluded_operators`` are **mutually exclusive**
    within a single rule.  ``allowed_operators = []`` disables all mutations
    for matched files.
    """

    glob: str
    allowed_operators: list[str] | None = None    # whitelist (empty = none)
    excluded_operators: list[str] | None = None   # blacklist
    _spec: pathspec.PathSpec = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.allowed_operators is not None and self.excluded_operators is not None:
            raise ValueError(
                f"Rule {self.glob!r}: allowed_operators and excluded_operators "
                "are mutually exclusive — use one or the other."
            )
        self._spec = pathspec.PathSpec.from_lines("gitwildmatch", [self.glob])

    def matches(self, filepath: str) -> bool:
        """Check if *filepath* matches this rule's glob pattern."""
        return self._spec.match_file(filepath.replace("\\", "/"))

    def filter_operators(self, operator_names: list[str]) -> list[str]:
        """Return the operator names allowed by this rule."""
        if self.allowed_operators is not None:
            # allowed_operators = [] → no mutations at all
            allowed = set(self.allowed_operators)
            return [n for n in operator_names if n in allowed]
        if self.excluded_operators is not None:
            excluded = set(self.excluded_operators)
            return [n for n in operator_names if n not in excluded]
        return operator_names


def operators_for_file(
    filepath: str,
    all_operator_names: list[str],
    rules: list[OperatorRule],
) -> list[str]:
    """Apply rules top-to-bottom; **last** matching rule wins."""
    result = all_operator_names
    for rule in rules:
        if rule.matches(filepath):
            result = rule.filter_operators(all_operator_names)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Config dataclass
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class CrisprConfig:
    """Merged configuration (file + CLI)."""

    command: str = "pytest -x -q --tb=no --no-header"
    timeout: float = 30.0
    workers: int = 1
    coverage: bool = False
    source_dirs: list[str] | None = None
    include: list[str] | None = None
    exclude: list[str] | None = None
    operators: list[str] | None = None
    no_cache: bool = False
    no_baseline: bool = False
    quiet: bool = False
    dry_run: bool = False
    json_report: str | None = None
    html_report: str | None = None
    junit_report: str | None = None
    rules: list[OperatorRule] = field(default_factory=list)
    ignore_patterns: list[str] = field(default_factory=list)  # regex patterns


# ═══════════════════════════════════════════════════════════════════════════
# TOML parsing
# ═══════════════════════════════════════════════════════════════════════════

def load_toml(path: Path) -> dict[str, Any]:
    """Load a TOML file, return empty dict on failure."""
    if not path.exists():
        return {}
    try:
        # Python 3.11+
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return {}

    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def load_config(
    config_path: Path | None = None,
    project_root: Path | None = None,
) -> CrisprConfig:
    """Load config from pyproject.toml (or a custom path)."""
    if config_path is None and project_root is not None:
        config_path = project_root / "pyproject.toml"
    if config_path is None:
        return CrisprConfig()

    data = load_toml(config_path)
    tool = data.get("tool", {}).get("crispr", {})
    if not tool:
        return CrisprConfig()

    # Parse rules
    raw_rules = tool.pop("rules", [])
    rules: list[OperatorRule] = []
    for r in raw_rules:
        if "glob" in r:
            allowed = r.get("allowed_operators")
            excluded = r.get("excluded_operators")
            rules.append(OperatorRule(
                glob=r["glob"],
                allowed_operators=allowed,
                excluded_operators=excluded,
            ))

    return CrisprConfig(
        command=tool.get("command", CrisprConfig.command),
        timeout=tool.get("timeout", CrisprConfig.timeout),
        workers=tool.get("workers", CrisprConfig.workers),
        coverage=tool.get("coverage", CrisprConfig.coverage),
        source_dirs=tool.get("source_dirs"),
        include=tool.get("include"),
        exclude=tool.get("exclude"),
        operators=tool.get("operators"),
        no_cache=tool.get("no_cache", False),
        no_baseline=tool.get("no_baseline", False),
        quiet=tool.get("quiet", False),
        json_report=tool.get("json"),
        html_report=tool.get("html"),
        junit_report=tool.get("junit"),
        rules=rules,
        ignore_patterns=tool.get("ignore_patterns", []),
    )


def merge_cli_over_config(
    cfg: CrisprConfig,
    cli_args: dict[str, Any],
) -> CrisprConfig:
    """CLI arguments override config file values (non-None CLI wins)."""
    # Map CLI arg names → config field names
    _MAP = {
        "command": "command",
        "timeout": "timeout",
        "workers": "workers",
        "coverage": "coverage",
        "source_dirs": "source_dirs",
        "include": "include",
        "exclude": "exclude",
        "operators": "operators",
        "no_cache": "no_cache",
        "no_baseline": "no_baseline",
        "quiet": "quiet",
        "dry_run": "dry_run",
        "json": "json_report",
        "html": "html_report",
        "junit": "junit_report",
    }

    for cli_name, cfg_name in _MAP.items():
        cli_val = cli_args.get(cli_name)
        if cli_val is None:
            continue
        # For booleans: CLI flag present means True, absence means don't override
        if isinstance(cli_val, bool) and not cli_val:
            continue
        # For lists: CLI provided means override
        # For strings/numbers: only override if different from argparse default
        setattr(cfg, cfg_name, cli_val)

    return cfg


# ═══════════════════════════════════════════════════════════════════════════
# Ignore patterns — regex-based survivor filtering
# ═══════════════════════════════════════════════════════════════════════════

def is_line_ignored(
    source_line: str,
    patterns: list[str],
) -> bool:
    """True if the source line matches any ignore pattern."""
    import re
    for pat in patterns:
        try:
            if re.search(pat, source_line):
                return True
        except re.error:
            continue
    return False


def get_source_line(source: str, lineno: int) -> str:
    """Extract a single source line (1-based)."""
    lines = source.splitlines()
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1]
    return ""
