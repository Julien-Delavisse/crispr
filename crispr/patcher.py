"""Surgical text-level mutation patcher.

Instead of ``ast.unparse()`` on the whole tree (which destroys formatting,
comments, and blank lines), this module splices *only* the mutated segment
into the original source text.  The result: minimal, readable diffs.

Strategy per mutation type
--------------------------
* **Token replacements** (arithmetic, comparison, boolean, augassign, unary,
  break/continue): locate the operator token inside the source span and swap it.
* **Constants**: replace the literal's source segment with the new value's repr.
* **Return → None**: replace the return-value portion with ``None``.
* **Statement deletion → pass**: replace the statement's line(s) with ``pass``.
* **Negate condition**: wrap the ``if`` test with ``not (...)``.
* **Decorator removal**: delete the decorator line.
* **Exception handler body → pass**: replace the body lines with ``pass``.
"""

from __future__ import annotations

import ast
import re
from typing import Sequence

from .operators import Mutation

# ── helpers ──────────────────────────────────────────────────────────────

def _lines(source: str) -> list[str]:
    """Split source into lines *with* newlines preserved."""
    return source.splitlines(keepends=True)


def _splice(
    source: str,
    start_line: int,   # 1-based
    start_col: int,    # 0-based
    end_line: int,     # 1-based
    end_col: int,      # 0-based
    replacement: str,
) -> str:
    """Replace the text span [start_line:start_col .. end_line:end_col) with *replacement*."""
    lines = _lines(source)
    # Build prefix (everything before the span)
    prefix_parts: list[str] = []
    for i in range(start_line - 1):
        prefix_parts.append(lines[i])
    prefix_parts.append(lines[start_line - 1][:start_col])

    # Build suffix (everything after the span)
    suffix_parts: list[str] = []
    suffix_parts.append(lines[end_line - 1][end_col:])
    for i in range(end_line, len(lines)):
        suffix_parts.append(lines[i])

    return "".join(prefix_parts) + replacement + "".join(suffix_parts)


def _delete_lines(source: str, start_line: int, end_line: int) -> str:
    """Delete lines [start_line .. end_line] (1-based, inclusive)."""
    lines = _lines(source)
    return "".join(lines[: start_line - 1] + lines[end_line:])


def _replace_lines(
    source: str,
    start_line: int,
    end_line: int,
    replacement: str,
) -> str:
    """Replace lines [start_line .. end_line] (1-based, inclusive) with *replacement*."""
    lines = _lines(source)
    indent = _get_indent(lines[start_line - 1])
    new_line = indent + replacement + "\n"
    return "".join(lines[: start_line - 1] + [new_line] + lines[end_line:])


def _get_indent(line: str) -> str:
    """Return the leading whitespace of a line."""
    return line[: len(line) - len(line.lstrip())]


def _node_span(node: ast.AST) -> tuple[int, int, int, int] | None:
    """Return (start_line, start_col, end_line, end_col) or None."""
    sl = getattr(node, "lineno", None)
    sc = getattr(node, "col_offset", None)
    el = getattr(node, "end_lineno", None)
    ec = getattr(node, "end_col_offset", None)
    if sl is not None and sc is not None and el is not None and ec is not None:
        return (sl, sc, el, ec)
    return None


# ── operator token maps ─────────────────────────────────────────────────

_OP_TOKEN: dict[type, str] = {
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
    ast.FloorDiv: "//", ast.Mod: "%", ast.Pow: "**",
    ast.UAdd: "+", ast.USub: "-",
}

_CMP_TOKEN: dict[type, str] = {
    ast.Eq: "==", ast.NotEq: "!=",
    ast.Lt: "<", ast.Gt: ">", ast.LtE: "<=", ast.GtE: ">=",
    ast.Is: "is", ast.IsNot: "is not",
    ast.In: "in", ast.NotIn: "not in",
}

_BOOL_TOKEN: dict[type, str] = {
    ast.And: "and", ast.Or: "or",
}


def _find_and_replace_token(
    source: str,
    start_line: int,
    start_col: int,
    end_line: int,
    end_col: int,
    old_token: str,
    new_token: str,
) -> str | None:
    """Find *old_token* within the source span and replace with *new_token*.

    Searches from the operator's likely position (between operands).
    Returns None if not found.
    """
    lines = _lines(source)
    # Extract the span text
    if start_line == end_line:
        span_text = lines[start_line - 1][start_col:end_col]
    else:
        parts = [lines[start_line - 1][start_col:]]
        for i in range(start_line, end_line - 1):
            parts.append(lines[i])
        parts.append(lines[end_line - 1][:end_col])
        span_text = "".join(parts)

    # For multi-word tokens like "is not", "not in", use word-boundary regex
    if " " in old_token:
        # "is not" → "is\\s+not", "not in" → "not\\s+in"
        pattern = re.escape(old_token).replace(r"\ ", r"\s+")
        match = re.search(pattern, span_text)
        if not match:
            return None
        idx = match.start()
        old_len = match.end() - match.start()
    else:
        idx = span_text.find(old_token)
        if idx == -1:
            return None
        old_len = len(old_token)

    # Convert span-relative offset back to absolute position
    # Walk through span_text to find the absolute line/col of the token
    abs_line = start_line
    abs_col = start_col
    for i in range(idx):
        if span_text[i] == "\n":
            abs_line += 1
            abs_col = 0
        else:
            abs_col += 1

    # Compute end of old token
    tok_end_line = abs_line
    tok_end_col = abs_col
    for i in range(old_len):
        ch = span_text[idx + i]
        if ch == "\n":
            tok_end_line += 1
            tok_end_col = 0
        else:
            tok_end_col += 1

    return _splice(source, abs_line, abs_col, tok_end_line, tok_end_col, new_token)


# ── public API ──────────────────────────────────────────────────────────

def apply_surgical(source: str, mutation: Mutation) -> str:
    """Apply *mutation* to *source* via surgical text patching.

    Falls back to ``ast.unparse`` if the surgical approach fails.
    """
    orig = mutation.original_node
    mutd = mutation.mutated_node
    op_name = mutation.operator

    try:
        result = _try_surgical(source, orig, mutd, op_name)
        if result is not None:
            return result
    except Exception:
        pass

    # Fallback: full AST unparse (noisy but correct)
    from .engine import apply_mutation
    return apply_mutation(source, mutation.file, mutation)


def _try_surgical(
    source: str,
    orig: ast.AST,
    mutd: ast.AST,
    op_name: str,
) -> str | None:
    """Try to apply the mutation surgically. Returns None if not possible."""

    span = _node_span(orig)

    # ── arithmetic: replace operator token ───────────────────────────
    if op_name == "arithmetic" and isinstance(orig, ast.BinOp) and isinstance(mutd, ast.BinOp):
        if span is None:
            return None
        old_tok = _OP_TOKEN.get(type(orig.op))
        new_tok = _OP_TOKEN.get(type(mutd.op))
        if old_tok and new_tok:
            return _find_and_replace_token(source, *span, old_tok, new_tok)

    # ── comparison: replace comparison token ─────────────────────────
    if op_name == "comparison" and isinstance(orig, ast.Compare) and isinstance(mutd, ast.Compare):
        if span is None:
            return None
        result = source
        # Find the changed comparator(s)
        for o_op, m_op in zip(orig.ops, mutd.ops):
            if type(o_op) != type(m_op):
                old_tok = _CMP_TOKEN.get(type(o_op))
                new_tok = _CMP_TOKEN.get(type(m_op))
                if old_tok and new_tok:
                    r = _find_and_replace_token(result, *span, old_tok, new_tok)
                    if r is not None:
                        result = r
        return result if result != source else None

    # ── boolean: replace and/or ──────────────────────────────────────
    if op_name == "boolean" and isinstance(orig, ast.BoolOp) and isinstance(mutd, ast.BoolOp):
        if span is None:
            return None
        old_tok = _BOOL_TOKEN.get(type(orig.op))
        new_tok = _BOOL_TOKEN.get(type(mutd.op))
        if old_tok and new_tok:
            return _find_and_replace_token(source, *span, old_tok, new_tok)

    # ── augmented assignment: += → -= etc ────────────────────────────
    if op_name == "aug_assign" and isinstance(orig, ast.AugAssign) and isinstance(mutd, ast.AugAssign):
        if span is None:
            return None
        old_tok = _OP_TOKEN.get(type(orig.op))
        new_tok = _OP_TOKEN.get(type(mutd.op))
        if old_tok and new_tok:
            return _find_and_replace_token(source, *span, old_tok + "=", new_tok + "=")

    # ── constant: replace the literal ────────────────────────────────
    if op_name == "constant" and isinstance(orig, ast.Constant) and isinstance(mutd, ast.Constant):
        if span is None:
            return None
        new_repr = repr(mutd.value)
        return _splice(source, span[0], span[1], span[2], span[3], new_repr)

    # ── return → None ────────────────────────────────────────────────
    if op_name == "return" and isinstance(orig, ast.Return):
        if span is None:
            return None
        # Replace everything after "return " with "None"
        lines = _lines(source)
        line_text = lines[span[0] - 1]
        ret_idx = line_text.find("return", span[1])
        if ret_idx == -1:
            return None
        after_return = ret_idx + len("return")
        return _splice(source, span[0], after_return, span[2], span[3], " None")

    # ── negate condition: if cond → if not cond ──────────────────────
    if op_name == "negate_condition" and isinstance(orig, ast.If):
        test_span = _node_span(orig.test)
        if test_span is None:
            return None
        # Get the original condition text
        test_source = ast.get_source_segment(source, orig.test)
        if test_source is None:
            return None
        # Wrap with "not (...)" for safety, or "not <expr>" for simple exprs
        if isinstance(orig.test, (ast.Name, ast.Constant, ast.Attribute, ast.Call)):
            replacement = f"not {test_source}"
        else:
            replacement = f"not ({test_source})"
        return _splice(source, test_span[0], test_span[1], test_span[2], test_span[3], replacement)

    # ── statement deletion → pass ────────────────────────────────────
    if op_name == "stmt_deletion":
        if span is None:
            return None
        return _replace_lines(source, span[0], span[2], "pass")

    # ── unary: +x ↔ -x, not x → x ──────────────────────────────────
    if op_name == "unary" and isinstance(orig, ast.UnaryOp):
        if span is None:
            return None
        if isinstance(mutd, ast.UnaryOp):
            # +x → -x or -x → +x
            old_tok = _OP_TOKEN.get(type(orig.op))
            new_tok = _OP_TOKEN.get(type(mutd.op))
            if old_tok and new_tok:
                return _splice(source, span[0], span[1], span[0], span[1] + len(old_tok), new_tok)
        else:
            # not x → x : remove "not " prefix
            operand_span = _node_span(orig.operand)
            if operand_span:
                operand_text = ast.get_source_segment(source, orig.operand)
                if operand_text:
                    return _splice(source, span[0], span[1], span[2], span[3], operand_text)

    # ── decorator removal ────────────────────────────────────────────
    if op_name == "decorator_removal" and isinstance(orig, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        # Find which decorator was removed by comparing lists
        if isinstance(mutd, type(orig)):
            orig_decos = orig.decorator_list
            mutd_decos = mutd.decorator_list
            if len(mutd_decos) == len(orig_decos) - 1:
                # Find the removed decorator
                for i, deco in enumerate(orig_decos):
                    kept = orig_decos[:i] + orig_decos[i + 1:]
                    # Check if mutd_decos matches kept (by type comparison)
                    if len(kept) == len(mutd_decos):
                        deco_span = _node_span(deco)
                        if deco_span:
                            # Delete the whole decorator line (including @)
                            return _delete_lines(source, deco_span[0], deco_span[2])
                        break

    # ── exception handler body → pass ────────────────────────────────
    if op_name == "exception_handler" and isinstance(orig, ast.ExceptHandler):
        if orig.body and isinstance(mutd, ast.ExceptHandler) and len(mutd.body) == 1:
            first = orig.body[0]
            last = orig.body[-1]
            first_span = _node_span(first)
            last_span = _node_span(last)
            if first_span and last_span:
                return _replace_lines(source, first_span[0], last_span[2], "pass")

    # ── break ↔ continue ────────────────────────────────────────────
    if op_name == "break_continue":
        if span is None:
            return None
        if isinstance(orig, ast.Break):
            return _splice(source, span[0], span[1], span[2], span[3], "continue")
        elif isinstance(orig, ast.Continue):
            return _splice(source, span[0], span[1], span[2], span[3], "break")

    return None
