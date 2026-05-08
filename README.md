# crispr

**Fast, zero-dependency, AST-based mutation testing for Python.**

An alternative to [mutmut](https://github.com/boxed/mutmut) focused on:

- **Pure AST** — no regex hacks, no source-level string replacement.
- **Zero runtime dependencies** — only stdlib (`ast`, `subprocess`, `argparse`).
- **12 mutation operators** covering arithmetic, comparisons, booleans, constants, return values, statement deletion, decorators, exception handlers, and more.
- **Pragma skip** — annotate lines with `# crispr: skip` or `# pragma: no mutate`.
- **JSON + HTML reports** — CI-friendly JSON output plus a dark-themed HTML dashboard.

## Installation

```bash
pip install -e .
```

## Quick start

```bash
# Run from project root (uses `pytest -x -q` by default)
crispr .

# Custom test command
crispr . -c "python -m pytest tests/ -x -q"

# Only specific operators
crispr . --operators arithmetic comparison constant

# Dry run — list mutations without executing tests
crispr . --dry-run

# Generate reports
crispr . --json report.json --html report.html
```

## Mutation operators

| Operator            | Example                          |
|---------------------|----------------------------------|
| `arithmetic`        | `a + b` → `a - b`               |
| `comparison`        | `x == y` → `x != y`             |
| `boolean`           | `a and b` → `a or b`            |
| `negate_condition`  | `if x:` → `if not x:`           |
| `constant`          | `42` → `43`, `""` → `"MUTATED"` |
| `return`            | `return x` → `return None`      |
| `stmt_deletion`     | `x = 1` → `pass`                |
| `aug_assign`        | `x += 1` → `x -= 1`            |
| `unary`             | `-x` → `+x`, `not x` → `x`     |
| `decorator_removal` | `@cache` removed                 |
| `exception_handler` | `except: ...` → `except: pass`  |
| `break_continue`    | `break` ↔ `continue`            |

## Skipping lines

```python
x = MAGIC_CONSTANT  # crispr: skip
y = OTHER_THING     # pragma: no mutate
```

## CLI options

```
crispr [path] [options]

  -c, --command CMD      Test command (default: pytest -x -q --tb=no --no-header)
  -i, --include PATTERN  Only mutate files matching these patterns
  -e, --exclude DIR      Exclude directories
  -o, --operators OP     Use only these operators
  -t, --timeout SEC      Per-mutation timeout (default: 30)
  -q, --quiet            Only show summary
  --no-baseline          Skip baseline test check
  --dry-run              List mutations without running
  --json FILE            Write JSON report
  --html FILE            Write HTML report
```

## License

MIT
