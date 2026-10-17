"""Safe function expressions for plots: parsed with `ast`, only whitelisted nodes, never `eval`.

Allowed: numbers, the variable x, constants pi and e, + - * / ** %, unary minus, and the functions
sin cos tan asin acos atan sinh cosh tanh exp log log10 sqrt abs floor ceil.
"""
from __future__ import annotations

import ast
import math
import operator
from typing import Callable

MAX_LEN = 80
FUNCS: dict[str, Callable[[float], float]] = {
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh, "exp": math.exp, "log": math.log, "ln": math.log, "log10": math.log10,
    "sqrt": math.sqrt, "abs": abs, "floor": math.floor, "ceil": math.ceil,
}
CONSTS = {"pi": math.pi, "e": math.e}
BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod}
UNOPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}


class ExprError(ValueError):
    pass


def parse(expr: str) -> ast.Expression:
    expr = (expr or "").strip().replace("^", "**")
    if not expr or len(expr) > MAX_LEN:
        raise ExprError(f"expression must be 1-{MAX_LEN} characters")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"not a valid expression: {e.msg}") from e
    func_names = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for node in ast.walk(tree):
        if id(node) in func_names:  # the function name itself is checked by its Call below
            continue
        if isinstance(node, (ast.Expression, ast.Load, ast.operator, ast.unaryop)):
            continue
        if isinstance(node, ast.BinOp) and type(node.op) in BINOPS:
            continue
        if isinstance(node, ast.UnaryOp) and type(node.op) in UNOPS:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            continue
        if isinstance(node, ast.Name) and (node.id == "x" or node.id in CONSTS):
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCS and len(node.args) == 1 and not node.keywords:
            continue
        raise ExprError(f"'{ast.unparse(node)[:30]}' is not allowed in a plot expression")
    return tree


def compile_expr(expr: str) -> Callable[[float], float]:
    tree = parse(expr)

    def ev(node, x: float) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body, x)
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.Name):
            return x if node.id == "x" else CONSTS[node.id]
        if isinstance(node, ast.BinOp):
            a, b = ev(node.left, x), ev(node.right, x)
            if isinstance(node.op, ast.Pow) and (abs(b) > 12 or abs(a) > 1e6):
                raise ArithmeticError("exponent too large")
            return BINOPS[type(node.op)](a, b)
        if isinstance(node, ast.UnaryOp):
            return UNOPS[type(node.op)](ev(node.operand, x))
        return FUNCS[node.func.id](ev(node.args[0], x))

    return lambda x: ev(tree, x)


def sample(expr: str, x_range: tuple[float, float], n: int = 160) -> list[tuple[float, float | None]]:
    """(x, y) points; y is None where the function is undefined (division by zero, log of a negative number, ...)."""
    f = compile_expr(expr)
    lo, hi = x_range
    pts = []
    for i in range(n + 1):
        x = lo + (hi - lo) * i / n
        try:
            y = f(x)
            pts.append((x, y if math.isfinite(y) else None))
        except (ValueError, ZeroDivisionError, OverflowError, ArithmeticError):
            pts.append((x, None))
    return pts
