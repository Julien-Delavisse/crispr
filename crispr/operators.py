"""Mutation operators that transform Python AST nodes.

Covers all standard mutmut operators plus extras:
  arithmetic, bitwise, comparison, comparison_boundary, boolean,
  negate_condition, constant, string_mutation, return, stmt_deletion,
  assign_to_none, aug_assign, unary, call_arg, string_method_swap,
  subscript, keyword_name, decorator_removal, exception_handler,
  break_continue
"""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class Mutation:
    """A single mutation applied to source code."""

    file: str
    lineno: int
    col_offset: int
    operator: str
    description: str
    original_node: ast.AST
    mutated_node: ast.AST
    id: str = ""  # stable hash, set by engine

    def __str__(self) -> str:
        short_id = self.id[:8] if self.id else "?"
        return f"[{short_id}] [{self.operator}] {self.file}:{self.lineno} — {self.description}"


# ═══════════════════════════════════════════════════════════════════════════
# Base
# ═══════════════════════════════════════════════════════════════════════════

class MutationOperator:
    name: str = "base"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        yield from ()


def _deep(node: ast.AST) -> ast.AST:
    return copy.deepcopy(node)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Arithmetic:  + ↔ -, * ↔ /, // → /, ** → *, % → /
# ═══════════════════════════════════════════════════════════════════════════

_ARITH_SWAPS: dict[type, list[type]] = {
    ast.Add: [ast.Sub],
    ast.Sub: [ast.Add],
    ast.Mult: [ast.Div],
    ast.Div: [ast.Mult],
    ast.FloorDiv: [ast.Div],
    ast.Mod: [ast.Div],
    ast.Pow: [ast.Mult],
}


class ArithmeticOperator(MutationOperator):
    name = "arithmetic"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if isinstance(node, ast.BinOp):
            for cls in _ARITH_SWAPS.get(type(node.op), []):
                m = _deep(node)
                m.op = cls()
                yield m


# ═══════════════════════════════════════════════════════════════════════════
# 2. Bitwise:  & ↔ |, ^ → &, << ↔ >>
# ═══════════════════════════════════════════════════════════════════════════

_BITWISE_SWAPS: dict[type, list[type]] = {
    ast.BitAnd: [ast.BitOr],
    ast.BitOr: [ast.BitAnd],
    ast.BitXor: [ast.BitAnd],
    ast.LShift: [ast.RShift],
    ast.RShift: [ast.LShift],
}


class BitwiseOperator(MutationOperator):
    name = "bitwise"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if isinstance(node, ast.BinOp):
            for cls in _BITWISE_SWAPS.get(type(node.op), []):
                m = _deep(node)
                m.op = cls()
                yield m


# ═══════════════════════════════════════════════════════════════════════════
# 3. Comparison inversion:  == ↔ !=, < ↔ >=, > ↔ <=, is ↔ is not, in ↔ not in
# ═══════════════════════════════════════════════════════════════════════════

_CMP_INVERT: dict[type, type] = {
    ast.Eq: ast.NotEq,  ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,    ast.GtE: ast.Lt,
    ast.Gt: ast.LtE,    ast.LtE: ast.Gt,
    ast.Is: ast.IsNot,   ast.IsNot: ast.Is,
    ast.In: ast.NotIn,   ast.NotIn: ast.In,
}


class ComparisonOperator(MutationOperator):
    name = "comparison"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if isinstance(node, ast.Compare):
            for idx, op in enumerate(node.ops):
                swap = _CMP_INVERT.get(type(op))
                if swap is not None:
                    m = _deep(node)
                    m.ops[idx] = swap()
                    yield m


# ═══════════════════════════════════════════════════════════════════════════
# 4. Comparison boundary shift:  < → <=, <= → <, > → >=, >= → >
# ═══════════════════════════════════════════════════════════════════════════

_CMP_BOUNDARY: dict[type, type] = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
}


class ComparisonBoundaryOperator(MutationOperator):
    name = "comparison_boundary"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if isinstance(node, ast.Compare):
            for idx, op in enumerate(node.ops):
                shift = _CMP_BOUNDARY.get(type(op))
                if shift is not None:
                    m = _deep(node)
                    m.ops[idx] = shift()
                    yield m


# ═══════════════════════════════════════════════════════════════════════════
# 5. Boolean:  and ↔ or
# ═══════════════════════════════════════════════════════════════════════════

class BooleanOperator(MutationOperator):
    name = "boolean"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if isinstance(node, ast.BoolOp):
            swap = {ast.And: ast.Or, ast.Or: ast.And}.get(type(node.op))
            if swap:
                m = _deep(node)
                m.op = swap()
                yield m


# ═══════════════════════════════════════════════════════════════════════════
# 6. Negate condition:  if x → if not x
# ═══════════════════════════════════════════════════════════════════════════

class NegateConditionOperator(MutationOperator):
    name = "negate_condition"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if isinstance(node, ast.If):
            m = _deep(node)
            m.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
            ast.copy_location(m.test, node.test)
            yield m


# ═══════════════════════════════════════════════════════════════════════════
# 7. Constants (numbers, bools, None, complex, strings basic)
# ═══════════════════════════════════════════════════════════════════════════

class ConstantMutator(MutationOperator):
    name = "constant"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if not isinstance(node, ast.Constant):
            return

        val = node.value

        if isinstance(val, bool):
            m = _deep(node); m.value = not val; yield m

        elif isinstance(val, int) and not isinstance(val, bool):
            for v in self._int_repls(val):
                m = _deep(node); m.value = v; yield m

        elif isinstance(val, float):
            for v in self._float_repls(val):
                m = _deep(node); m.value = v; yield m

        elif isinstance(val, complex):
            # 1j → 2j
            m = _deep(node); m.value = val + (1j if val.imag >= 0 else -1j); yield m

        elif val is None:
            # None → "" and None → 0
            m1 = _deep(node); m1.value = ""; yield m1
            m2 = _deep(node); m2.value = 0; yield m2

        elif isinstance(val, str):
            # Basic: non-empty → "", empty → "MUTATED"
            if val:
                m = _deep(node); m.value = ""; yield m
            else:
                m = _deep(node); m.value = "MUTATED"; yield m

    @staticmethod
    def _int_repls(val: int) -> list[int]:
        return [1] if val == 0 else [val + 1]

    @staticmethod
    def _float_repls(val: float) -> list[float]:
        return [1.0] if val == 0.0 else [val + 1.0]


# ═══════════════════════════════════════════════════════════════════════════
# 8. String enriched mutations: XX-padding, case swap
# ═══════════════════════════════════════════════════════════════════════════

class StringMutationOperator(MutationOperator):
    name = "string_mutation"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            return
        val = node.value
        if not val:
            return

        # XX-padding: "foo" → "XXfooXX"
        m = _deep(node); m.value = f"XX{val}XX"; yield m

        # Case mutations (only if the string has cased characters)
        lower = val.lower()
        upper = val.upper()
        if lower != val:
            m = _deep(node); m.value = lower; yield m
        if upper != val:
            m = _deep(node); m.value = upper; yield m


# ═══════════════════════════════════════════════════════════════════════════
# 9. Return:  return x → return None
# ═══════════════════════════════════════════════════════════════════════════

class ReturnMutator(MutationOperator):
    name = "return"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if isinstance(node, ast.Return) and node.value is not None:
            m = _deep(node)
            m.value = ast.Constant(value=None)
            ast.copy_location(m.value, node.value)
            yield m


# ═══════════════════════════════════════════════════════════════════════════
# 10. Statement deletion:  statement → pass
# ═══════════════════════════════════════════════════════════════════════════

class StatementDeletion(MutationOperator):
    name = "stmt_deletion"
    _TARGETS = (ast.Assign, ast.AugAssign, ast.Expr, ast.Delete, ast.Raise)

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if isinstance(node, self._TARGETS):
            p = ast.Pass()
            ast.copy_location(p, node)
            yield p


# ═══════════════════════════════════════════════════════════════════════════
# 11. Assignment → None:  a = expr → a = None
# ═══════════════════════════════════════════════════════════════════════════

class AssignToNone(MutationOperator):
    name = "assign_to_none"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if isinstance(node, ast.Assign) and node.value is not None:
            # Skip if already None
            if isinstance(node.value, ast.Constant) and node.value.value is None:
                return
            m = _deep(node)
            m.value = ast.Constant(value=None)
            ast.copy_location(m.value, node.value)
            yield m


# ═══════════════════════════════════════════════════════════════════════════
# 12. Augmented assignment extended:
#     += ↔ -=, *= ↔ /=, //= → /=, %= → /=, **= → *=
#     <<= ↔ >>=, &= ↔ |=, ^= → &=
#     Also: x += val → x = val  (drop augmented)
# ═══════════════════════════════════════════════════════════════════════════

_AUG_SWAPS: dict[type, type] = {
    ast.Add: ast.Sub,    ast.Sub: ast.Add,
    ast.Mult: ast.Div,   ast.Div: ast.Mult,
    ast.FloorDiv: ast.Div, ast.Mod: ast.Div, ast.Pow: ast.Mult,
    ast.LShift: ast.RShift, ast.RShift: ast.LShift,
    ast.BitAnd: ast.BitOr, ast.BitOr: ast.BitAnd, ast.BitXor: ast.BitAnd,
}


class AugAssignOperator(MutationOperator):
    name = "aug_assign"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if not isinstance(node, ast.AugAssign):
            return

        # Swap operator
        swap = _AUG_SWAPS.get(type(node.op))
        if swap is not None:
            m = _deep(node)
            m.op = swap()
            yield m

        # Drop augmented: x += val → x = val
        assign = ast.Assign(
            targets=[_deep(node.target)],
            value=_deep(node.value),
        )
        ast.copy_location(assign, node)
        # Copy end positions
        for attr in ("end_lineno", "end_col_offset"):
            if hasattr(node, attr):
                setattr(assign, attr, getattr(node, attr))
        yield assign


# ═══════════════════════════════════════════════════════════════════════════
# 13. Unary:  +x ↔ -x,  not x → x,  ~x → x
# ═══════════════════════════════════════════════════════════════════════════

class UnaryOperator(MutationOperator):
    name = "unary"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if not isinstance(node, ast.UnaryOp):
            return
        if isinstance(node.op, ast.UAdd):
            m = _deep(node); m.op = ast.USub(); yield m
        elif isinstance(node.op, ast.USub):
            m = _deep(node); m.op = ast.UAdd(); yield m
        elif isinstance(node.op, ast.Not):
            yield _deep(node.operand)
        elif isinstance(node.op, ast.Invert):
            # ~x → x
            yield _deep(node.operand)


# ═══════════════════════════════════════════════════════════════════════════
# 14. Function call argument mutations:
#     arg → None, arg removal (if ≥2 args)
# ═══════════════════════════════════════════════════════════════════════════

class CallArgMutator(MutationOperator):
    name = "call_arg"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if not isinstance(node, ast.Call):
            return
        if not node.args:
            return

        for i, arg in enumerate(node.args):
            # Skip if already None
            if isinstance(arg, ast.Constant) and arg.value is None:
                continue

            # arg → None
            m = _deep(node)
            none_node = ast.Constant(value=None)
            ast.copy_location(none_node, arg)
            m.args[i] = none_node
            yield m

            # Remove arg (only if there are ≥2 args or there are kwargs)
            if len(node.args) >= 2 or node.keywords:
                m2 = _deep(node)
                m2.args = m2.args[:i] + m2.args[i + 1:]
                yield m2


# ═══════════════════════════════════════════════════════════════════════════
# 15. String method swaps:
#     lower ↔ upper, lstrip ↔ rstrip, find ↔ rfind,
#     index ↔ rindex, ljust ↔ rjust, split ↔ rsplit,
#     removeprefix ↔ removesuffix, partition ↔ rpartition
# ═══════════════════════════════════════════════════════════════════════════

_METHOD_SWAPS: dict[str, str] = {
    "lower": "upper",        "upper": "lower",
    "lstrip": "rstrip",      "rstrip": "lstrip",
    "find": "rfind",         "rfind": "find",
    "index": "rindex",       "rindex": "index",
    "ljust": "rjust",        "rjust": "ljust",
    "split": "rsplit",       "rsplit": "split",
    "removeprefix": "removesuffix",  "removesuffix": "removeprefix",
    "partition": "rpartition",       "rpartition": "partition",
}


class StringMethodSwap(MutationOperator):
    name = "string_method_swap"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if not isinstance(node, ast.Call):
            return
        func = node.func
        if not isinstance(func, ast.Attribute):
            return
        swap = _METHOD_SWAPS.get(func.attr)
        if swap is not None:
            m = _deep(node)
            m.func.attr = swap
            yield m


# ═══════════════════════════════════════════════════════════════════════════
# 16. Subscript / slice mutations:
#     s[0] → s[1],  s[n] → s[n+1],  s[n:] → s[n+1:]
# ═══════════════════════════════════════════════════════════════════════════

class SubscriptMutator(MutationOperator):
    name = "subscript"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if not isinstance(node, ast.Subscript):
            return

        sl = node.slice

        # Simple index: s[n] → s[n+1]
        if isinstance(sl, ast.Constant) and isinstance(sl.value, int):
            m = _deep(node)
            m.slice.value = sl.value + 1
            yield m

        # Slice mutations
        elif isinstance(sl, ast.Slice):
            # s[lower:...] → s[lower+1:...]
            if sl.lower is not None and isinstance(sl.lower, ast.Constant) and isinstance(sl.lower.value, int):
                m = _deep(node)
                m.slice.lower.value = sl.lower.value + 1
                yield m
            # s[...:upper] → s[...:upper+1]
            if sl.upper is not None and isinstance(sl.upper, ast.Constant) and isinstance(sl.upper.value, int):
                m = _deep(node)
                m.slice.upper.value = sl.upper.value + 1
                yield m


# ═══════════════════════════════════════════════════════════════════════════
# 17. Keyword argument name mutation:  func(key=val) → func(keyXX=val)
# ═══════════════════════════════════════════════════════════════════════════

class KeywordNameMutator(MutationOperator):
    name = "keyword_name"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if not isinstance(node, ast.Call):
            return
        for i, kw in enumerate(node.keywords):
            if kw.arg is not None:  # skip **kwargs
                m = _deep(node)
                m.keywords[i].arg = kw.arg + "XX"
                yield m


# ═══════════════════════════════════════════════════════════════════════════
# 18. Decorator removal
# ═══════════════════════════════════════════════════════════════════════════

class DecoratorRemoval(MutationOperator):
    name = "decorator_removal"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for idx in range(len(node.decorator_list)):
                m = _deep(node)
                m.decorator_list = node.decorator_list[:idx] + node.decorator_list[idx + 1:]
                yield m


# ═══════════════════════════════════════════════════════════════════════════
# 19. Exception handler body → pass
# ═══════════════════════════════════════════════════════════════════════════

class ExceptionHandlerMutator(MutationOperator):
    name = "exception_handler"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if isinstance(node, ast.ExceptHandler):
            if len(node.body) > 1 or not isinstance(node.body[0], ast.Pass):
                m = _deep(node)
                p = ast.Pass()
                ast.copy_location(p, node.body[0])
                m.body = [p]
                yield m


# ═══════════════════════════════════════════════════════════════════════════
# 20. Break ↔ Continue
# ═══════════════════════════════════════════════════════════════════════════

class BreakContinueSwap(MutationOperator):
    name = "break_continue"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if isinstance(node, ast.Break):
            r = ast.Continue()
            ast.copy_location(r, node)
            yield r
        elif isinstance(node, ast.Continue):
            r = ast.Break()
            ast.copy_location(r, node)
            yield r


# ═══════════════════════════════════════════════════════════════════════════
# 21. If/else branch swap:  if c: A else: B  →  if c: B else: A
# ═══════════════════════════════════════════════════════════════════════════

class IfElseSwap(MutationOperator):
    name = "if_else_swap"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if isinstance(node, ast.If) and node.orelse:
            m = _deep(node)
            m.body, m.orelse = m.orelse, m.body
            yield m


# ═══════════════════════════════════════════════════════════════════════════
# 22. Ternary swap:  a if cond else b  →  b if cond else a
# ═══════════════════════════════════════════════════════════════════════════

class TernarySwap(MutationOperator):
    name = "ternary_swap"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if isinstance(node, ast.IfExp):
            m = _deep(node)
            m.body, m.orelse = m.orelse, m.body
            yield m


# ═══════════════════════════════════════════════════════════════════════════
# 23. Default parameter mutation:
#     def f(x=10) → def f(x=11), def f(x=None)
#     Skip defaults that are function calls (could raise at import time)
# ═══════════════════════════════════════════════════════════════════════════

class DefaultParamMutator(MutationOperator):
    name = "default_param"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return
        defaults = node.args.defaults
        if not defaults:
            return

        for i, default in enumerate(defaults):
            # Skip function calls as defaults — mutating could crash at import
            if isinstance(default, ast.Call):
                continue

            if isinstance(default, ast.Constant):
                val = default.value
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    m = _deep(node)
                    m.args.defaults[i].value = val + 1
                    yield m
                elif isinstance(val, str):
                    m = _deep(node)
                    m.args.defaults[i].value = f"XX{val}XX" if val else "MUTATED"
                    yield m
                elif isinstance(val, bool):
                    m = _deep(node)
                    m.args.defaults[i].value = not val
                    yield m

            # Also: default → None (unless already None)
            if not (isinstance(default, ast.Constant) and default.value is None):
                m = _deep(node)
                m.args.defaults[i] = ast.Constant(value=None)
                ast.copy_location(m.args.defaults[i], default)
                yield m


# ═══════════════════════════════════════════════════════════════════════════
# 24. Comprehension filter removal:
#     [x for x in y if cond]  →  [x for x in y]
# ═══════════════════════════════════════════════════════════════════════════

class ComprehensionFilterRemoval(MutationOperator):
    name = "comp_filter"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if not isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            return
        for gen_idx, gen in enumerate(node.generators):
            for if_idx in range(len(gen.ifs)):
                m = _deep(node)
                m.generators[gen_idx].ifs = (
                    gen.ifs[:if_idx] + gen.ifs[if_idx + 1:]
                )
                yield m


# ═══════════════════════════════════════════════════════════════════════════
# 25. Dict method swaps:
#     .keys() ↔ .values(),  .pop() ↔ .get()
# ═══════════════════════════════════════════════════════════════════════════

_DICT_METHOD_SWAPS: dict[str, str] = {
    "keys": "values",     "values": "keys",
    "pop": "get",         "get": "pop",
}


class DictMethodSwap(MutationOperator):
    name = "dict_method_swap"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if not isinstance(node, ast.Call):
            return
        func = node.func
        if not isinstance(func, ast.Attribute):
            return
        swap = _DICT_METHOD_SWAPS.get(func.attr)
        if swap is not None:
            m = _deep(node)
            m.func.attr = swap
            yield m


# ═══════════════════════════════════════════════════════════════════════════
# 26. Yield mutation:  yield x → yield None
# ═══════════════════════════════════════════════════════════════════════════

class YieldMutator(MutationOperator):
    name = "yield"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if isinstance(node, ast.Yield) and node.value is not None:
            m = _deep(node)
            m.value = ast.Constant(value=None)
            ast.copy_location(m.value, node.value)
            yield m
        elif isinstance(node, ast.YieldFrom):
            # yield from iterable → yield from []
            m = _deep(node)
            m.value = ast.List(elts=[], ctx=ast.Load())
            ast.copy_location(m.value, node.value)
            yield m


# ═══════════════════════════════════════════════════════════════════════════
# 27. Exception type widening:
#     except ValueError → except Exception
# ═══════════════════════════════════════════════════════════════════════════

class ExceptionTypeWidening(MutationOperator):
    name = "exception_widen"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if not isinstance(node, ast.ExceptHandler):
            return
        if node.type is None:
            return  # bare except — already widest
        # Don't widen if already Exception or BaseException
        if isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException"):
            return
        m = _deep(node)
        m.type = ast.Name(id="Exception", ctx=ast.Load())
        ast.copy_location(m.type, node.type)
        yield m


# ═══════════════════════════════════════════════════════════════════════════
# 28. Assert removal:  assert cond  →  pass
# ═══════════════════════════════════════════════════════════════════════════

class AssertRemoval(MutationOperator):
    name = "assert_removal"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if isinstance(node, ast.Assert):
            p = ast.Pass()
            ast.copy_location(p, node)
            yield p


# ═══════════════════════════════════════════════════════════════════════════
# 29. Remove await:  await f()  →  f()
# ═══════════════════════════════════════════════════════════════════════════

class RemoveAwait(MutationOperator):
    name = "remove_await"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if isinstance(node, ast.Await):
            # Drop the await, return just the inner expression
            yield _deep(node.value)


# ═══════════════════════════════════════════════════════════════════════════
# 30. Range boundary:  range(n) → range(n±1)
# ═══════════════════════════════════════════════════════════════════════════

class RangeBoundary(MutationOperator):
    name = "range_boundary"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if not isinstance(node, ast.Call):
            return
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "range"):
            return
        if not node.args:
            return

        # Mutate the last positional arg (the stop value, or start if 1-arg)
        target_idx = min(len(node.args) - 1, 1) if len(node.args) >= 2 else 0
        target = node.args[target_idx]

        # range(n) → range(n + 1)
        m1 = _deep(node)
        m1.args[target_idx] = ast.BinOp(
            left=_deep(target), op=ast.Add(), right=ast.Constant(value=1)
        )
        ast.copy_location(m1.args[target_idx], target)
        yield m1

        # range(n) → range(n - 1)
        m2 = _deep(node)
        m2.args[target_idx] = ast.BinOp(
            left=_deep(target), op=ast.Sub(), right=ast.Constant(value=1)
        )
        ast.copy_location(m2.args[target_idx], target)
        yield m2


# ═══════════════════════════════════════════════════════════════════════════
# 31. F-string expression mutation:  f"{x:.2f}" → f"{x:.3f}"
#     Also: f"{expr}" → f"" (remove expression entirely)
# ═══════════════════════════════════════════════════════════════════════════

class FStringMutator(MutationOperator):
    name = "fstring"

    def mutate(self, node: ast.AST) -> Iterator[ast.AST]:
        if not isinstance(node, ast.JoinedStr):
            return
        # f"...{expr}..." → f"" (remove all interpolation)
        if any(isinstance(v, ast.FormattedValue) for v in node.values):
            m = _deep(node)
            m.values = [ast.Constant(value="")]
            yield m

        # Mutate individual format specs: .2f → .3f, etc.
        for i, val in enumerate(node.values):
            if isinstance(val, ast.FormattedValue) and val.format_spec is not None:
                if isinstance(val.format_spec, ast.JoinedStr):
                    for j, spec_part in enumerate(val.format_spec.values):
                        if isinstance(spec_part, ast.Constant) and isinstance(spec_part.value, str):
                            mutated_spec = self._mutate_format_spec(spec_part.value)
                            if mutated_spec and mutated_spec != spec_part.value:
                                m = _deep(node)
                                m.values[i].format_spec.values[j].value = mutated_spec
                                yield m

    @staticmethod
    def _mutate_format_spec(spec: str) -> str | None:
        """Try to shift precision or width in a format spec."""
        import re
        # Match patterns like .2f, .3g, .4e, 10d, etc.
        match = re.search(r"\.(\d+)", spec)
        if match:
            n = int(match.group(1))
            return spec[:match.start(1)] + str(n + 1) + spec[match.end(1):]
        match = re.search(r"(\d+)", spec)
        if match:
            n = int(match.group(1))
            return spec[:match.start(1)] + str(n + 1) + spec[match.end(1):]
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════════════════════

ALL_OPERATORS: list[MutationOperator] = [
    # Core
    ArithmeticOperator(),
    BitwiseOperator(),
    ComparisonOperator(),
    ComparisonBoundaryOperator(),
    BooleanOperator(),
    NegateConditionOperator(),
    ConstantMutator(),
    StringMutationOperator(),
    # Statements
    ReturnMutator(),
    StatementDeletion(),
    AssignToNone(),
    AugAssignOperator(),
    UnaryOperator(),
    AssertRemoval(),
    # Calls
    CallArgMutator(),
    StringMethodSwap(),
    DictMethodSwap(),
    KeywordNameMutator(),
    RangeBoundary(),
    # Subscripts
    SubscriptMutator(),
    # Control flow
    IfElseSwap(),
    TernarySwap(),
    BreakContinueSwap(),
    # Functions / generators
    DefaultParamMutator(),
    YieldMutator(),
    RemoveAwait(),
    # Comprehensions
    ComprehensionFilterRemoval(),
    # Structure
    DecoratorRemoval(),
    ExceptionHandlerMutator(),
    ExceptionTypeWidening(),
    # Strings
    FStringMutator(),
]


def get_operators(names: list[str] | None = None) -> list[MutationOperator]:
    """Return operators filtered by *names*, or all if None."""
    if names is None:
        return list(ALL_OPERATORS)
    name_set = set(names)
    return [op for op in ALL_OPERATORS if op.name in name_set]
