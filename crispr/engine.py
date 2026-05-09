"""Engine that discovers and applies mutations to Python source files."""

from __future__ import annotations

import ast
import copy
import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .cache import mutation_id as _make_id
from .operators import ALL_OPERATORS, Mutation, MutationOperator


@dataclass(frozen=True)
class MutationResult:
    """Outcome of running the test suite against one mutation."""

    mutation: Mutation
    status: str  # "killed" | "survived" | "timeout" | "error" | "cached"
    duration_s: float = 0.0
    output: str = ""
    diff: str = ""


# ---------------------------------------------------------------------------
# AST walker — yields (node, parent, field_name, index) for every node
# ---------------------------------------------------------------------------

def _walk_with_parent(
    tree: ast.AST,
) -> Iterator[tuple[ast.AST, ast.AST | None, str | None, int | None]]:
    """Depth-first walk yielding (node, parent, field_name, field_index)."""
    stack: list[tuple[ast.AST, ast.AST | None, str | None, int | None]] = [
        (tree, None, None, None)
    ]
    while stack:
        node, parent, fname, fidx = stack.pop()
        yield node, parent, fname, fidx
        for name, value in ast.iter_fields(node):
            if isinstance(value, list):
                for i, item in enumerate(value):
                    if isinstance(item, ast.AST):
                        stack.append((item, node, name, i))
            elif isinstance(value, ast.AST):
                stack.append((value, node, name, None))


# ---------------------------------------------------------------------------
# Filtering helpers
# ---------------------------------------------------------------------------

def _is_test_file(path: Path) -> bool:
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def _is_dunder(node: ast.AST) -> bool:
    """True if node is a __dunder__ method/attr definition we should skip."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name.startswith("__") and node.name.endswith("__")
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def discover_files(
    root: Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[Path]:
    """Find all .py files under *root*, excluding tests and common dirs."""
    exclude_dirs = {
        "__pycache__", ".git", ".tox", ".venv", "venv", "env",
        "node_modules", ".mypy_cache", ".pytest_cache", ".eggs",
        "build", "dist", "*.egg-info",
    }
    if exclude:
        exclude_dirs.update(exclude)

    files: list[Path] = []
    for py in sorted(root.rglob("*.py")):
        if any(part in exclude_dirs for part in py.parts):
            continue
        rel_dirs = py.relative_to(root).parts[:-1]
        if any(p.startswith(".") for p in rel_dirs):
            continue
        if _is_test_file(py):
            continue
        if include:
            if not any(pattern in str(py) for pattern in include):
                continue
        files.append(py)
    return files


def generate_mutations(
    source: str,
    filepath: str,
    operators: list[MutationOperator] | None = None,
    skip_lines: set[int] | None = None,
    covered_lines: set[int] | None = None,
    pragma_skips: dict[int, frozenset[str] | None] | None = None,
) -> list[Mutation]:
    """Parse *source* and return all possible mutations.

    Parameters
    ----------
    skip_lines:
        Lines to skip entirely (e.g. uncovered lines from coverage.py).
    covered_lines:
        If provided, only mutate lines present in this set.
    pragma_skips:
        Per-line skip rules from ``# pragma: no mutate`` comments.
        ``None`` value → skip all operators; ``frozenset`` → skip those operators.
    """
    ops = operators or ALL_OPERATORS
    skip = skip_lines or set()
    pragmas = pragma_skips or {}

    try:
        tree = ast.parse(source, filename=filepath)
    except SyntaxError:
        return []

    mutations: list[Mutation] = []

    for node, _parent, _fname, _fidx in _walk_with_parent(tree):
        lineno = getattr(node, "lineno", 0)
        col = getattr(node, "col_offset", 0)

        if lineno in skip:
            continue
        if _is_dunder(node):
            continue
        if covered_lines is not None and lineno not in covered_lines:
            continue

        # Check pragma: None = skip all, frozenset = skip selective
        pragma = pragmas.get(lineno)
        if pragma is None and lineno in pragmas:
            continue  # skip all operators on this line

        for op in ops:
            # Selective pragma: skip this operator if it's in the exclusion set
            if isinstance(pragma, frozenset) and op.name in pragma:
                continue

            for mutated_node in op.mutate(node):
                desc = _describe(op, node, mutated_node)
                mid = _make_id(filepath, op.name, lineno, col, desc)
                mutations.append(
                    Mutation(
                        file=filepath,
                        lineno=lineno,
                        col_offset=col,
                        operator=op.name,
                        description=desc,
                        original_node=node,
                        mutated_node=mutated_node,
                        id=mid,
                    )
                )

    return mutations


def apply_mutation(source: str, filepath: str, mutation: Mutation) -> str:
    """Apply a mutation by surgical source-text replacement.

    Instead of unparsing the whole AST (which destroys formatting), we locate
    the *exact* text span of the affected node and splice in the replacement.
    """
    span = _replacement_span(source, mutation)
    if span is None:
        # Fallback to full AST unparse (should be rare)
        return _apply_mutation_ast(source, filepath, mutation)

    start_line, start_col, end_line, end_col, replacement = span
    return _splice(source, start_line, start_col, end_line, end_col, replacement)


# ---------------------------------------------------------------------------
# Surgical span computation — per operator type
# ---------------------------------------------------------------------------

def _replacement_span(
    source: str, mutation: Mutation,
) -> tuple[int, int, int, int, str] | None:
    """Return (start_line, start_col, end_line, end_col, replacement_text).

    Lines are 1-based (matching AST conventions).  Returns ``None`` when the
    positions are unavailable and we must fall back to the AST approach.
    """
    orig = mutation.original_node
    mutated = mutation.mutated_node
    op = mutation.operator

    # Bail out if the node lacks end-position info
    end_ln = getattr(orig, "end_lineno", None)
    end_col = getattr(orig, "end_col_offset", None)
    if end_ln is None or end_col is None:
        return None

    lines = source.splitlines(keepends=True)

    # ------------------------------------------------------------------
    # negate_condition  —  only patch the *test*, not the whole if-block
    # ------------------------------------------------------------------
    if op == "negate_condition" and isinstance(orig, ast.If):
        test = orig.test
        t_end_ln = getattr(test, "end_lineno", None)
        t_end_col = getattr(test, "end_col_offset", None)
        if t_end_ln is None or t_end_col is None:
            return None
        original_text = _extract_span(lines, test.lineno, test.col_offset, t_end_ln, t_end_col)
        # Wrap in not (…) — use parens only if needed
        if _needs_parens(test):
            replacement = f"not ({original_text})"
        else:
            replacement = f"not {original_text}"
        return (test.lineno, test.col_offset, t_end_ln, t_end_col, replacement)

    # ------------------------------------------------------------------
    # decorator_removal  —  delete one decorator line
    # ------------------------------------------------------------------
    if op == "decorator_removal" and isinstance(orig, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        # Find which decorator was removed
        removed_idx = _find_removed_decorator(orig, mutated)
        if removed_idx is not None:
            dec = orig.decorator_list[removed_idx]
            dec_end_ln = getattr(dec, "end_lineno", None)
            dec_end_col = getattr(dec, "end_col_offset", None)
            if dec_end_ln is None:
                return None
            # Remove the entire decorator line (including the @ and newline)
            # The decorator node points at the expression after @, but the
            # @ itself is on the same line at a lower column.
            dec_line_idx = dec.lineno - 1  # 0-based
            if dec_line_idx < len(lines):
                # Remove entire line(s) of the decorator
                return (dec.lineno, 0, dec_end_ln, len(lines[dec_end_ln - 1].rstrip("\n")), "")
        return None

    # ------------------------------------------------------------------
    # exception_handler  —  replace body with pass
    # ------------------------------------------------------------------
    if op == "exception_handler" and isinstance(orig, ast.ExceptHandler):
        first = orig.body[0]
        last = orig.body[-1]
        last_end_ln = getattr(last, "end_lineno", None)
        last_end_col = getattr(last, "end_col_offset", None)
        if last_end_ln is None:
            return None
        indent = _get_indent(lines, first.lineno)
        return (first.lineno, 0, last_end_ln, last_end_col, f"{indent}pass")

    # ------------------------------------------------------------------
    # stmt_deletion  —  replace statement with pass (preserve indent)
    # ------------------------------------------------------------------
    if op == "stmt_deletion":
        indent = _get_indent(lines, orig.lineno)
        return (orig.lineno, 0, end_ln, end_col, f"{indent}pass")

    # ------------------------------------------------------------------
    # return  —  replace  return <value>  with  return None
    # ------------------------------------------------------------------
    if op == "return" and isinstance(orig, ast.Return) and orig.value is not None:
        val = orig.value
        val_end_ln = getattr(val, "end_lineno", None)
        val_end_col = getattr(val, "end_col_offset", None)
        if val_end_ln is None:
            return None
        return (val.lineno, val.col_offset, val_end_ln, val_end_col, "None")

    # ------------------------------------------------------------------
    # break_continue  —  swap keyword
    # ------------------------------------------------------------------
    if op == "break_continue":
        if isinstance(orig, ast.Break):
            return (orig.lineno, orig.col_offset, end_ln, end_col, "continue")
        elif isinstance(orig, ast.Continue):
            return (orig.lineno, orig.col_offset, end_ln, end_col, "break")

    # ------------------------------------------------------------------
    # assign_to_none  —  replace only the value in  a = <value>
    # ------------------------------------------------------------------
    if op == "assign_to_none" and isinstance(orig, ast.Assign) and orig.value is not None:
        val = orig.value
        val_end_ln = getattr(val, "end_lineno", None)
        val_end_col = getattr(val, "end_col_offset", None)
        if val_end_ln is None:
            return None
        return (val.lineno, val.col_offset, val_end_ln, val_end_col, "None")

    # ------------------------------------------------------------------
    # aug_assign (drop augmented): x += v → x = v
    # Replace the whole statement to change += to =
    # ------------------------------------------------------------------
    if op == "aug_assign" and isinstance(orig, ast.AugAssign) and isinstance(mutated, ast.Assign):
        indent = _get_indent(lines, orig.lineno)
        replacement = ast.unparse(mutated)
        return (orig.lineno, 0, end_ln, end_col, f"{indent}{replacement}")

    # ------------------------------------------------------------------
    # string_method_swap  —  only change the method name
    # ------------------------------------------------------------------
    if op == "string_method_swap" and isinstance(orig, ast.Call):
        func = orig.func
        if isinstance(func, ast.Attribute):
            # The method name starts right after the '.'
            # func.end_lineno/end_col_offset point to end of attr name
            # func.col_offset points to start of the whole a.method
            # We need the position of just the method name
            attr_end_ln = getattr(func, "end_lineno", None)
            attr_end_col = getattr(func, "end_col_offset", None)
            if attr_end_ln is not None and attr_end_col is not None:
                attr_start_col = attr_end_col - len(func.attr)
                new_attr = mutated.func.attr if isinstance(mutated, ast.Call) and isinstance(mutated.func, ast.Attribute) else None
                if new_attr:
                    return (attr_end_ln, attr_start_col, attr_end_ln, attr_end_col, new_attr)

    # ------------------------------------------------------------------
    # keyword_name  —  only change the keyword name
    # ------------------------------------------------------------------
    if op == "keyword_name" and isinstance(orig, ast.Call) and isinstance(mutated, ast.Call):
        for ok, mk in zip(orig.keywords, mutated.keywords):
            if ok.arg != mk.arg and ok.arg is not None:
                kw_ln = getattr(ok, "lineno", None)
                kw_col = getattr(ok, "col_offset", None)
                if kw_ln is not None and kw_col is not None:
                    # keyword col_offset points to the start of the keyword name
                    return (kw_ln, kw_col, kw_ln, kw_col + len(ok.arg), mk.arg)
                break

    # ------------------------------------------------------------------
    # if_else_swap  —  must use AST fallback (swaps multi-line bodies)
    # ------------------------------------------------------------------
    if op == "if_else_swap":
        return None  # force AST fallback

    # ------------------------------------------------------------------
    # default_param  —  replace only the specific default value
    # ------------------------------------------------------------------
    if op == "default_param" and isinstance(orig, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for od, md in zip(orig.args.defaults, mutated.args.defaults):
            if ast.dump(od) != ast.dump(md):
                od_end_ln = getattr(od, "end_lineno", None)
                od_end_col = getattr(od, "end_col_offset", None)
                if od_end_ln is not None and od_end_col is not None:
                    return (od.lineno, od.col_offset, od_end_ln, od_end_col, ast.unparse(md))
                break
        return None

    # ------------------------------------------------------------------
    # yield  —  replace only the yielded value
    # ------------------------------------------------------------------
    if op == "yield" and isinstance(orig, ast.Yield) and orig.value is not None:
        val = orig.value
        val_end_ln = getattr(val, "end_lineno", None)
        val_end_col = getattr(val, "end_col_offset", None)
        if val_end_ln is None:
            return None
        return (val.lineno, val.col_offset, val_end_ln, val_end_col, "None")

    # ------------------------------------------------------------------
    # exception_widen  —  replace only the exception type
    # ------------------------------------------------------------------
    if op == "exception_widen" and isinstance(orig, ast.ExceptHandler) and orig.type is not None:
        t = orig.type
        t_end_ln = getattr(t, "end_lineno", None)
        t_end_col = getattr(t, "end_col_offset", None)
        if t_end_ln is None:
            return None
        return (t.lineno, t.col_offset, t_end_ln, t_end_col, "Exception")

    # ------------------------------------------------------------------
    # assert_removal  —  replace with pass (like stmt_deletion)
    # ------------------------------------------------------------------
    if op == "assert_removal":
        indent = _get_indent(lines, orig.lineno)
        return (orig.lineno, 0, end_ln, end_col, f"{indent}pass")

    # ------------------------------------------------------------------
    # remove_await  —  drop the 'await' keyword, keep the expression
    # ------------------------------------------------------------------
    if op == "remove_await" and isinstance(orig, ast.Await):
        val = orig.value
        val_text = _extract_span(
            lines, val.lineno, val.col_offset,
            getattr(val, "end_lineno", val.lineno),
            getattr(val, "end_col_offset", val.col_offset),
        )
        return (orig.lineno, orig.col_offset, end_ln, end_col, val_text)

    # ------------------------------------------------------------------
    # dict_method_swap  —  reuse string_method_swap logic
    # ------------------------------------------------------------------
    if op == "dict_method_swap" and isinstance(orig, ast.Call):
        func = orig.func
        if isinstance(func, ast.Attribute):
            attr_end_ln = getattr(func, "end_lineno", None)
            attr_end_col = getattr(func, "end_col_offset", None)
            if attr_end_ln is not None and attr_end_col is not None:
                attr_start_col = attr_end_col - len(func.attr)
                new_attr = mutated.func.attr if isinstance(mutated, ast.Call) and isinstance(mutated.func, ast.Attribute) else None
                if new_attr:
                    return (attr_end_ln, attr_start_col, attr_end_ln, attr_end_col, new_attr)

    # ------------------------------------------------------------------
    # General case: unparse only the mutated sub-expression/statement
    # ------------------------------------------------------------------
    replacement = ast.unparse(mutated)
    return (orig.lineno, orig.col_offset, end_ln, end_col, replacement)


# ---------------------------------------------------------------------------
# Text-splicing helper
# ---------------------------------------------------------------------------

def _splice(
    source: str,
    start_line: int, start_col: int,
    end_line: int, end_col: int,
    replacement: str,
) -> str:
    """Replace the text span [start_line:start_col .. end_line:end_col) with *replacement*."""
    lines = source.splitlines(keepends=True)

    # Handle edge: empty file
    if not lines:
        return replacement

    # Ensure lines are within bounds
    sl = start_line - 1  # to 0-based
    el = end_line - 1

    # Build prefix (everything before the span)
    prefix = "".join(lines[:sl]) + lines[sl][:start_col]

    # Build suffix (everything after the span)
    if el < len(lines):
        suffix = lines[el][end_col:] + "".join(lines[el + 1:])
    else:
        suffix = ""

    # Handle the case where we're deleting a whole line (replacement is empty)
    if not replacement and prefix.endswith(("\n", "\r\n", "\r")):
        # Don't leave a blank line — absorb the trailing newline of the deleted line
        if suffix.startswith(("\n", "\r\n", "\r")):
            suffix = suffix.lstrip("\n\r")

    return prefix + replacement + suffix


def _extract_span(
    lines: list[str],
    start_line: int, start_col: int,
    end_line: int, end_col: int,
) -> str:
    """Extract the source text in the given span."""
    if start_line == end_line:
        return lines[start_line - 1][start_col:end_col]

    parts = [lines[start_line - 1][start_col:]]
    for ln in range(start_line, end_line - 1):  # middle lines
        parts.append(lines[ln])
    parts.append(lines[end_line - 1][:end_col])
    return "".join(parts)


def _get_indent(lines: list[str], lineno: int) -> str:
    """Return the whitespace prefix of a line."""
    if lineno < 1 or lineno > len(lines):
        return ""
    line = lines[lineno - 1]
    return line[: len(line) - len(line.lstrip())]


def _needs_parens(node: ast.AST) -> bool:
    """True if wrapping the node in ``not`` requires parentheses."""
    return isinstance(node, (ast.BoolOp, ast.IfExp, ast.Lambda, ast.NamedExpr))


def _find_removed_decorator(
    orig: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    mutated: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> int | None:
    """Find which decorator index was removed."""
    if len(mutated.decorator_list) != len(orig.decorator_list) - 1:
        return None
    for i, dec in enumerate(orig.decorator_list):
        # Check if this decorator is missing in the mutated version
        if i >= len(mutated.decorator_list) or ast.dump(dec) != ast.dump(mutated.decorator_list[i]):
            return i
    return len(orig.decorator_list) - 1  # last one removed


# ---------------------------------------------------------------------------
# AST fallback (old approach)
# ---------------------------------------------------------------------------

def _apply_mutation_ast(source: str, filepath: str, mutation: Mutation) -> str:
    """Fallback: full-file AST unparse. Destroys formatting but always works."""
    tree = ast.parse(source, filename=filepath)

    class _Replacer(ast.NodeTransformer):
        _replaced = False

        def generic_visit(self, node: ast.AST) -> ast.AST:
            if self._replaced:
                return node
            if (
                getattr(node, "lineno", None) == mutation.lineno
                and getattr(node, "col_offset", None) == mutation.col_offset
                and type(node) == type(mutation.original_node)
            ):
                self._replaced = True
                ast.copy_location(mutation.mutated_node, node)
                return mutation.mutated_node
            return super().generic_visit(node)

    replacer = _Replacer()
    new_tree = replacer.visit(tree)
    ast.fix_missing_locations(new_tree)
    return ast.unparse(new_tree)


def make_diff(source: str, mutated_source: str, filepath: str) -> str:
    """Create a unified diff between original and mutated source."""
    original_lines = source.splitlines(keepends=True)
    mutated_lines = mutated_source.splitlines(keepends=True)
    diff_lines = difflib.unified_diff(
        original_lines,
        mutated_lines,
        fromfile=f"a/{filepath}",
        tofile=f"b/{filepath} (mutated)",
        n=2,
    )
    return "".join(diff_lines)


def parse_pragma_skips(source: str) -> dict[int, frozenset[str] | None]:
    """Parse pragma comments to build per-line skip rules.

    Returns ``{lineno: operators_to_skip}`` where:

    - ``None`` means skip **all** operators on that line.
    - ``frozenset[str]`` means skip only those specific operators.

    Recognised syntax::

        x = 1  # pragma: no mutate              → skip all
        x = 1  # crispr: skip                 → skip all
        x = 1  # pragma: no mutate[constant]    → skip only "constant"
        x = 1  # pragma: no mutate[constant,arithmetic]  → skip both
    """
    import re

    skips: dict[int, frozenset[str] | None] = {}
    # Matches: # pragma: no mutate  or  # crispr: skip
    # Optionally followed by [op1,op2,...]
    _PATTERN = re.compile(
        r"#\s*(?:pragma:\s*no\s*mutate|crispr:\s*skip)"
        r"(?:\[([a-z_,\s]+)\])?"
    )

    for i, line in enumerate(source.splitlines(), start=1):
        m = _PATTERN.search(line)
        if m:
            ops_str = m.group(1)
            if ops_str:
                ops = frozenset(o.strip() for o in ops_str.split(",") if o.strip())
                skips[i] = ops
            else:
                skips[i] = None

    return skips


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _op_symbol(node: ast.AST) -> str:
    """Human-readable symbol for an operator AST node."""
    _MAP = {
        ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
        ast.FloorDiv: "//", ast.Mod: "%", ast.Pow: "**",
        ast.BitAnd: "&", ast.BitOr: "|", ast.BitXor: "^",
        ast.LShift: "<<", ast.RShift: ">>",
        ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.Gt: ">",
        ast.LtE: "<=", ast.GtE: ">=", ast.Is: "is", ast.IsNot: "is not",
        ast.In: "in", ast.NotIn: "not in",
        ast.And: "and", ast.Or: "or",
        ast.UAdd: "+", ast.USub: "-", ast.Not: "not", ast.Invert: "~",
    }
    return _MAP.get(type(node), type(node).__name__)


def _describe(op: MutationOperator, original: ast.AST, mutated: ast.AST) -> str:
    """Build a human-readable description of the mutation."""
    # BinOp (arithmetic + bitwise)
    if isinstance(original, ast.BinOp) and isinstance(mutated, ast.BinOp):
        return f"{_op_symbol(original.op)} → {_op_symbol(mutated.op)}"

    # Compare (inversion + boundary)
    if isinstance(original, ast.Compare) and isinstance(mutated, ast.Compare):
        parts = []
        for o, m in zip(original.ops, mutated.ops):
            if type(o) != type(m):
                parts.append(f"{_op_symbol(o)} → {_op_symbol(m)}")
        return ", ".join(parts) or "comparison mutated"

    # BoolOp
    if isinstance(original, ast.BoolOp) and isinstance(mutated, ast.BoolOp):
        return f"{_op_symbol(original.op)} → {_op_symbol(mutated.op)}"

    # Constant
    if isinstance(original, ast.Constant) and isinstance(mutated, ast.Constant):
        ov, mv = original.value, mutated.value
        # Truncate long strings
        ov_s = repr(ov) if not isinstance(ov, str) or len(ov) <= 30 else repr(ov[:27] + "...")
        mv_s = repr(mv) if not isinstance(mv, str) or len(mv) <= 30 else repr(mv[:27] + "...")
        return f"{ov_s} → {mv_s}"

    # Return
    if isinstance(original, ast.Return):
        return "return value → None"

    # AugAssign → AugAssign (operator swap)
    if isinstance(original, ast.AugAssign) and isinstance(mutated, ast.AugAssign):
        return f"{_op_symbol(original.op)}= → {_op_symbol(mutated.op)}="

    # AugAssign → Assign (drop augmented)
    if isinstance(original, ast.AugAssign) and isinstance(mutated, ast.Assign):
        return f"{_op_symbol(original.op)}= → ="

    # Unary
    if isinstance(original, ast.UnaryOp) and isinstance(mutated, ast.UnaryOp):
        return f"{_op_symbol(original.op)}x → {_op_symbol(mutated.op)}x"
    if isinstance(original, ast.UnaryOp) and not isinstance(mutated, ast.UnaryOp):
        return f"{_op_symbol(original.op)}x → x"

    # Call-based mutations
    if isinstance(original, ast.Call) and isinstance(mutated, ast.Call):
        # String method swap
        if (isinstance(original.func, ast.Attribute) and isinstance(mutated.func, ast.Attribute)
                and original.func.attr != mutated.func.attr):
            return f".{original.func.attr}() → .{mutated.func.attr}()"
        # Keyword name mutation
        for ok, mk in zip(original.keywords, mutated.keywords if len(mutated.keywords) == len(original.keywords) else []):
            if ok.arg != mk.arg:
                return f"kwarg {ok.arg}= → {mk.arg}="
        # Arg removal
        if len(mutated.args) < len(original.args):
            return "argument removed"
        # Arg → None
        for oa, ma in zip(original.args, mutated.args):
            if isinstance(ma, ast.Constant) and ma.value is None and not (isinstance(oa, ast.Constant) and oa.value is None):
                return "argument → None"

    # Assign → None
    if op.name == "assign_to_none":
        return "value → None"

    if op.name == "negate_condition":
        return "if cond → if not cond"
    if op.name == "stmt_deletion":
        return "statement → pass"
    if op.name == "decorator_removal":
        return "decorator removed"
    if op.name == "exception_handler":
        return "except body → pass"
    if op.name == "break_continue":
        return f"{type(original).__name__.lower()} → {type(mutated).__name__.lower()}"

    # Subscript
    if op.name == "subscript" and isinstance(original, ast.Subscript):
        sl = original.slice
        ml = mutated.slice if isinstance(mutated, ast.Subscript) else None
        if isinstance(sl, ast.Constant) and ml and isinstance(ml, ast.Constant):
            return f"[{sl.value}] → [{ml.value}]"
        if isinstance(sl, ast.Slice) and ml and isinstance(ml, ast.Slice):
            return "slice boundary shifted"

    # If/else swap
    if op.name == "if_else_swap":
        return "if/else branches swapped"

    # Ternary swap
    if op.name == "ternary_swap" and isinstance(original, ast.IfExp):
        return "ternary branches swapped"

    # Default param
    if op.name == "default_param":
        if isinstance(original, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Find which default changed
            for od, md in zip(original.args.defaults, mutated.args.defaults):
                if ast.dump(od) != ast.dump(md):
                    if isinstance(od, ast.Constant) and isinstance(md, ast.Constant):
                        return f"default {od.value!r} → {md.value!r}"
                    elif isinstance(md, ast.Constant) and md.value is None:
                        return "default → None"
        return "default parameter mutated"

    # Comprehension filter removal
    if op.name == "comp_filter":
        return "filter removed from comprehension"

    # Dict method swap
    if op.name == "dict_method_swap" and isinstance(original, ast.Call):
        if isinstance(original.func, ast.Attribute) and isinstance(mutated, ast.Call) and isinstance(mutated.func, ast.Attribute):
            return f".{original.func.attr}() → .{mutated.func.attr}()"

    # Yield
    if op.name == "yield":
        if isinstance(original, ast.YieldFrom):
            return "yield from → yield from []"
        return "yield value → None"

    # Exception widening
    if op.name == "exception_widen":
        if isinstance(original, ast.ExceptHandler) and original.type is not None:
            orig_name = original.type.id if isinstance(original.type, ast.Name) else "..."
            return f"except {orig_name} → except Exception"

    # Assert removal
    if op.name == "assert_removal":
        return "assert → pass"

    # Remove await
    if op.name == "remove_await":
        return "await expr → expr"

    # Range boundary
    if op.name == "range_boundary":
        return "range() boundary ±1"

    # F-string
    if op.name == "fstring":
        if isinstance(mutated, ast.JoinedStr) and len(mutated.values) == 1:
            return "f-string → empty"
        return "f-string format mutated"

    return f"{op.name} mutation"
