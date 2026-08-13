"""选股表达式安全求值。

支持形如 `close > SMA(close, 20) and RSI(close, 14) < 30` 的表达式：
- 标识符白名单：OHLCV 列名 + INDICATOR_FUNCS 注册的指标函数
- 仅允许比较/算术/布尔运算与函数调用，ast 校验节点类型，绝不使用裸 eval
- 求值结果取时间序列最后一个值（对"当前时点"判断）
"""

import ast

import pandas as pd

from app.screener.indicators import INDICATOR_FUNCS

_ALLOWED_COLUMNS = {"open", "high", "low", "close", "volume"}

_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.Call, ast.Name, ast.Load, ast.Constant, ast.keyword,
)


class ExprError(ValueError):
    pass


def validate_expr(expr: str) -> ast.Expression:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"表达式语法错误: {e}") from e
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ExprError(f"表达式包含不允许的语法: {type(node).__name__}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in INDICATOR_FUNCS:
                raise ExprError("只允许调用内置指标函数: " + ", ".join(sorted(INDICATOR_FUNCS)))
        if isinstance(node, ast.Name) and node.id not in _ALLOWED_COLUMNS | set(INDICATOR_FUNCS):
            raise ExprError(f"未知标识符: {node.id}（允许: {sorted(_ALLOWED_COLUMNS)} 及指标函数）")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            raise ExprError("常量只允许数字")
    return tree


def _eval_node(node: ast.AST, bars: pd.DataFrame):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, bars)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in _ALLOWED_COLUMNS:
            return bars[node.id]
        return INDICATOR_FUNCS[node.id]
    if isinstance(node, ast.Call):
        func = INDICATOR_FUNCS[node.func.id]
        args = [_eval_node(a, bars) for a in node.args]
        kwargs = {kw.arg: _eval_node(kw.value, bars) for kw in node.keywords}
        return func(*args, **kwargs)
    if isinstance(node, ast.UnaryOp):
        val = _eval_node(node.operand, bars)
        return ~val if isinstance(node.op, ast.Not) else -val
    if isinstance(node, ast.BinOp):
        left, right = _eval_node(node.left, bars), _eval_node(node.right, bars)
        ops = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
               ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
               ast.Mod: lambda a, b: a % b, ast.Pow: lambda a, b: a ** b}
        return ops[type(node.op)](left, right)
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, bars)
        result = None
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval_node(comparator, bars)
            ops = {ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
                   ast.Lt: lambda a, b: a < b, ast.LtE: lambda a, b: a <= b,
                   ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b}
            piece = ops[type(op)](left, right)
            result = piece if result is None else (result & piece)
            left = right
        return result
    if isinstance(node, ast.BoolOp):
        values = [_eval_node(v, bars) for v in node.values]
        out = values[0]
        for v in values[1:]:
            out = (out & v) if isinstance(node.op, ast.And) else (out | v)
        return out
    raise ExprError(f"无法求值的节点: {type(node).__name__}")


def eval_expr_last(expr: str, bars: pd.DataFrame) -> bool:
    """在单个标的的日线上求值，返回最后时点的布尔结果。"""
    tree = validate_expr(expr)
    result = _eval_node(tree, bars)
    if isinstance(result, pd.Series):
        if result.empty:
            return False
        val = result.iloc[-1]
        return bool(val) if pd.notna(val) else False
    return bool(result)
