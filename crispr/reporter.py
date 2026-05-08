"""Report generation for mutation testing results."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .engine import MutationResult


# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

class _C:
    """ANSI color codes (disabled when not a TTY)."""

    _enabled = sys.stdout.isatty()

    RESET = "\033[0m" if _enabled else ""
    BOLD = "\033[1m" if _enabled else ""
    DIM = "\033[2m" if _enabled else ""
    RED = "\033[31m" if _enabled else ""
    GREEN = "\033[32m" if _enabled else ""
    YELLOW = "\033[33m" if _enabled else ""
    CYAN = "\033[36m" if _enabled else ""
    MAGENTA = "\033[35m" if _enabled else ""
    BG_RED = "\033[41m" if _enabled else ""
    BG_GREEN = "\033[42m" if _enabled else ""


_STATUS_STYLE = {
    "killed": f"{_C.GREEN}KILLED{_C.RESET}",
    "survived": f"{_C.RED}SURVIVED{_C.RESET}",
    "timeout": f"{_C.YELLOW}TIMEOUT{_C.RESET}",
    "error": f"{_C.MAGENTA}ERROR{_C.RESET}",
    "cached": f"{_C.CYAN}CACHED{_C.RESET}",
    "ignored": f"{_C.DIM}IGNORED{_C.RESET}",
}


# ---------------------------------------------------------------------------
# Summary dataclass
# ---------------------------------------------------------------------------

@dataclass
class Summary:
    total: int = 0
    killed: int = 0
    survived: int = 0
    timeout: int = 0
    error: int = 0
    cached: int = 0
    ignored: int = 0
    skipped_no_coverage: int = 0
    duration_s: float = 0.0
    results_by_file: dict[str, list[MutationResult]] = field(default_factory=dict)

    @property
    def score(self) -> float:
        tested = self.killed + self.survived
        if tested == 0:
            return 100.0
        return (self.killed / tested) * 100

    @classmethod
    def from_results(cls, results: list[MutationResult]) -> Summary:
        s = cls()
        for r in results:
            s.total += 1
            if r.status == "killed":
                s.killed += 1
            elif r.status == "survived":
                s.survived += 1
            elif r.status == "timeout":
                s.timeout += 1
            elif r.status == "cached":
                s.cached += 1
                s.killed += 1
            elif r.status == "ignored":
                s.ignored += 1
            else:
                s.error += 1
            s.duration_s += r.duration_s

            fkey = r.mutation.file
            s.results_by_file.setdefault(fkey, []).append(r)
        return s


# ---------------------------------------------------------------------------
# Terminal reporter
# ---------------------------------------------------------------------------

def print_progress(current: int, total: int, result: MutationResult) -> None:
    """Print a single-line progress update."""
    pct = (current / total * 100) if total else 0
    status_str = _STATUS_STYLE.get(result.status, result.status)
    short_id = result.mutation.id[:8] if result.mutation.id else "--------"
    loc = f"{result.mutation.file}:{result.mutation.lineno}"
    desc = result.mutation.description
    print(
        f"  [{current:>4}/{total}] {pct:5.1f}%  {_C.DIM}{short_id}{_C.RESET}  "
        f"{status_str:<22}  {_C.DIM}{loc:<40}{_C.RESET}  {desc}"
    )


def print_summary(summary: Summary, show_diff: bool = True) -> None:
    """Print the final summary to stdout."""
    bar = "═" * 60
    print(f"\n{_C.BOLD}{bar}{_C.RESET}")
    print(f"{_C.BOLD}  MUTATION TESTING RESULTS{_C.RESET}")
    print(f"{_C.BOLD}{bar}{_C.RESET}\n")

    # Score with color
    score = summary.score
    if score >= 80:
        score_color = _C.GREEN
    elif score >= 60:
        score_color = _C.YELLOW
    else:
        score_color = _C.RED

    print(f"  Mutation score: {score_color}{_C.BOLD}{score:.1f}%{_C.RESET}")
    print(f"  Total mutations: {summary.total}")
    print(
        f"  {_C.GREEN}Killed: {summary.killed}{_C.RESET}  │  "
        f"{_C.RED}Survived: {summary.survived}{_C.RESET}  │  "
        f"{_C.YELLOW}Timeout: {summary.timeout}{_C.RESET}  │  "
        f"{_C.MAGENTA}Error: {summary.error}{_C.RESET}"
    )
    if summary.cached > 0:
        print(f"  {_C.CYAN}Cache hits: {summary.cached}{_C.RESET} (skipped re-testing)")
    if summary.ignored > 0:
        print(f"  {_C.DIM}Ignored: {summary.ignored} (matched ignore_patterns){_C.RESET}")
    if summary.skipped_no_coverage > 0:
        print(
            f"  {_C.DIM}Skipped (no coverage): {summary.skipped_no_coverage}{_C.RESET}"
        )
    print(f"  Duration: {summary.duration_s:.1f}s\n")

    # Survivors detail with diffs
    survivors = [
        r for r in _all_results(summary) if r.status == "survived"
    ]
    if survivors:
        print(f"  {_C.RED}{_C.BOLD}Surviving mutations ({len(survivors)}):{_C.RESET}\n")
        for r in survivors:
            short_id = r.mutation.id[:8] if r.mutation.id else "?"
            print(
                f"    {_C.RED}●{_C.RESET} {_C.DIM}{short_id}{_C.RESET}  "
                f"{r.mutation.file}:{r.mutation.lineno}  "
                f"[{r.mutation.operator}] {r.mutation.description}"
            )
            if show_diff and r.diff:
                for line in r.diff.splitlines():
                    if line.startswith("+") and not line.startswith("+++"):
                        print(f"      {_C.GREEN}{line}{_C.RESET}")
                    elif line.startswith("-") and not line.startswith("---"):
                        print(f"      {_C.RED}{line}{_C.RESET}")
                    elif line.startswith("@@"):
                        print(f"      {_C.CYAN}{line}{_C.RESET}")
                    else:
                        print(f"      {_C.DIM}{line}{_C.RESET}")
                print()
        print()

    # Per-file breakdown
    if len(summary.results_by_file) > 1:
        print(f"  {_C.BOLD}Per-file breakdown:{_C.RESET}\n")
        for fpath, file_results in sorted(summary.results_by_file.items()):
            fsum = Summary.from_results(file_results)
            sc = fsum.score
            if sc >= 80:
                fc = _C.GREEN
            elif sc >= 60:
                fc = _C.YELLOW
            else:
                fc = _C.RED
            print(
                f"    {fpath:<45} {fc}{sc:5.1f}%{_C.RESET}  "
                f"({fsum.killed}k / {fsum.survived}s / {fsum.total}t)"
            )
        print()

    print(f"{_C.BOLD}{bar}{_C.RESET}")


# ---------------------------------------------------------------------------
# JSON report
# ---------------------------------------------------------------------------

def write_json_report(summary: Summary, path: Path) -> None:
    """Write a machine-readable JSON report."""
    data = {
        "score": round(summary.score, 2),
        "total": summary.total,
        "killed": summary.killed,
        "survived": summary.survived,
        "timeout": summary.timeout,
        "error": summary.error,
        "cached": summary.cached,
        "duration_s": round(summary.duration_s, 2),
        "mutations": [
            {
                "id": r.mutation.id,
                "file": r.mutation.file,
                "line": r.mutation.lineno,
                "operator": r.mutation.operator,
                "description": r.mutation.description,
                "status": r.status,
                "duration_s": round(r.duration_s, 3),
                "diff": r.diff if r.status == "survived" else "",
            }
            for r in _all_results(summary)
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

def write_html_report(summary: Summary, path: Path) -> None:
    """Write a self-contained HTML report."""
    import html as html_mod

    rows = ""
    for r in _all_results(summary):
        css = {
            "killed": "color:#22c55e",
            "survived": "color:#ef4444;font-weight:bold",
            "timeout": "color:#eab308",
            "error": "color:#a855f7",
            "cached": "color:#06b6d4",
        }.get(r.status, "")
        short_id = r.mutation.id[:8] if r.mutation.id else ""
        diff_html = ""
        if r.status == "survived" and r.diff:
            diff_escaped = html_mod.escape(r.diff)
            diff_html = f'<pre class="diff">{diff_escaped}</pre>'
        rows += (
            f"<tr>"
            f'<td class="mono">{short_id}</td>'
            f"<td>{r.mutation.file}</td>"
            f"<td>{r.mutation.lineno}</td>"
            f"<td>{r.mutation.operator}</td>"
            f"<td>{r.mutation.description}</td>"
            f'<td style="{css}">{r.status.upper()}</td>'
            f"<td>{r.duration_s:.2f}s</td>"
            f"</tr>\n"
        )
        if diff_html:
            rows += f'<tr><td colspan="7">{diff_html}</td></tr>\n'

    score = summary.score
    score_color = "#22c55e" if score >= 80 else "#eab308" if score >= 60 else "#ef4444"

    cache_stat = ""
    if summary.cached > 0:
        cache_stat = f'<div class="stat"><div class="n" style="color:#06b6d4">{summary.cached}</div>Cached</div>'

    html_content = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>crispr report</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:system-ui,-apple-system,sans-serif; background:#0f172a; color:#e2e8f0; padding:2rem; }}
  h1 {{ font-size:1.5rem; margin-bottom:1rem; }}
  .score {{ font-size:3rem; font-weight:800; color:{score_color}; }}
  .stats {{ display:flex; gap:2rem; margin:1rem 0 2rem; flex-wrap:wrap; }}
  .stat {{ background:#1e293b; padding:1rem 1.5rem; border-radius:0.5rem; }}
  .stat .n {{ font-size:1.5rem; font-weight:700; }}
  table {{ width:100%; border-collapse:collapse; background:#1e293b; border-radius:0.5rem; overflow:hidden; }}
  th {{ background:#334155; padding:0.75rem 1rem; text-align:left; font-size:0.85rem; text-transform:uppercase; letter-spacing:0.05em; }}
  td {{ padding:0.6rem 1rem; border-top:1px solid #334155; font-size:0.9rem; }}
  td.mono {{ font-family:monospace; font-size:0.8rem; color:#94a3b8; }}
  tr:hover td {{ background:#334155; }}
  .diff {{ background:#0d1117; padding:0.75rem 1rem; border-radius:0.375rem; font-size:0.8rem;
           font-family:monospace; white-space:pre-wrap; margin:0.25rem 0; color:#8b949e;
           border-left:3px solid #ef4444; }}
</style>
</head>
<body>
<h1>crispr — mutation testing report</h1>
<div class="score">{score:.1f}%</div>
<div class="stats">
  <div class="stat"><div class="n">{summary.total}</div>Total</div>
  <div class="stat"><div class="n" style="color:#22c55e">{summary.killed}</div>Killed</div>
  <div class="stat"><div class="n" style="color:#ef4444">{summary.survived}</div>Survived</div>
  <div class="stat"><div class="n" style="color:#eab308">{summary.timeout}</div>Timeout</div>
  <div class="stat"><div class="n" style="color:#a855f7">{summary.error}</div>Error</div>
  {cache_stat}
</div>
<table>
<thead><tr><th>ID</th><th>File</th><th>Line</th><th>Operator</th><th>Description</th><th>Status</th><th>Time</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
</body>
</html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_content, encoding="utf-8")


# ---------------------------------------------------------------------------
# JUnit XML report
# ---------------------------------------------------------------------------

def write_junit_report(summary: Summary, path: Path) -> None:
    """Write a JUnit XML report compatible with CI systems.

    Mapping:  killed → passed, survived → failure,
    timeout → error, ignored → skipped.
    """
    from xml.etree.ElementTree import Element, SubElement, tostring
    from xml.dom.minidom import parseString

    results = _all_results(summary)
    failures = sum(1 for r in results if r.status == "survived")
    errors = sum(1 for r in results if r.status in ("timeout", "error"))
    skipped = sum(1 for r in results if r.status == "ignored")

    suites = Element("testsuites")
    suite = SubElement(suites, "testsuite",
        name="crispr",
        tests=str(len(results)),
        failures=str(failures),
        errors=str(errors),
        skipped=str(skipped),
        time=f"{summary.duration_s:.2f}",
    )

    for r in results:
        mid = r.mutation.id[:8] if r.mutation.id else "?"
        name = f"[{r.mutation.operator}] {r.mutation.description}"
        tc = SubElement(suite, "testcase",
            name=name,
            classname=r.mutation.file,
            time=f"{r.duration_s:.3f}",
        )
        # Add mutation ID as a property
        props = SubElement(tc, "properties")
        SubElement(props, "property", name="mutation_id", value=r.mutation.id or "")
        SubElement(props, "property", name="line", value=str(r.mutation.lineno))

        if r.status == "survived":
            fail = SubElement(tc, "failure",
                message=f"Mutation survived: {r.mutation.description}",
                type="MutationSurvived",
            )
            fail.text = r.diff or f"{r.mutation.file}:{r.mutation.lineno} [{r.mutation.operator}] {r.mutation.description}"

        elif r.status in ("timeout", "error"):
            err = SubElement(tc, "error",
                message=f"Mutation {r.status}",
                type="MutationTimeout" if r.status == "timeout" else "MutationError",
            )
            err.text = r.output[:500] if r.output else ""

        elif r.status == "ignored":
            SubElement(tc, "skipped", message="Matched ignore_patterns")

    # Pretty-print
    raw = tostring(suites, encoding="unicode", xml_declaration=True)
    pretty = parseString(raw).toprettyxml(indent="  ", encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pretty)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _all_results(summary: Summary) -> list[MutationResult]:
    results: list[MutationResult] = []
    for file_results in summary.results_by_file.values():
        results.extend(file_results)
    return results
