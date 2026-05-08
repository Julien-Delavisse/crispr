"""Generate unified diffs for mutations — surgical, minimal output."""
from __future__ import annotations
import difflib
from .engine import Mutation, apply_mutation


def mutation_diff(source: str, filepath: str, mutation: Mutation, context_lines: int = 2) -> str:
    """Return a focused unified diff for a single mutation."""
    try:
        mutated = apply_mutation(source, filepath, mutation)
    except Exception as exc:
        return f"(could not generate diff: {exc})"

    if source == mutated:
        return ""

    original_lines = source.splitlines(keepends=True)
    mutated_lines = mutated.splitlines(keepends=True)

    diff = difflib.unified_diff(
        original_lines,
        mutated_lines,
        fromfile=f"a/{filepath}",
        tofile=f"b/{filepath}",
        n=context_lines,
    )
    return "".join(diff)


def colorize_diff(diff_text: str) -> str:
    """Add ANSI colors to a unified diff."""
    lines = []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("+++") or line.startswith("---"):
            lines.append(f"\033[1m{line}\033[0m")
        elif line.startswith("@@"):
            lines.append(f"\033[36m{line}\033[0m")
        elif line.startswith("+"):
            lines.append(f"\033[32m{line}\033[0m")
        elif line.startswith("-"):
            lines.append(f"\033[31m{line}\033[0m")
        else:
            lines.append(line)
    return "".join(lines)
