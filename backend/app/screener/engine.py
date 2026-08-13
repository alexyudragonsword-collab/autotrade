"""选股引擎：universe → 基本面过滤 → 技术面过滤 → 排序/截断 → 落库。"""

import logging
from datetime import date, timedelta

import pandas as pd
from sqlalchemy.orm import Session

from app.data.store import get_bar_store, get_provider
from app.db.models import ScreenerConfig, ScreenResult
from app.domain.enums import Market
from app.screener.expressions import ExprError, eval_expr_last, validate_expr
from app.screener.indicators import INDICATOR_FUNCS

logger = logging.getLogger(__name__)

_FUND_OPS = {
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
}
# 技术面回看窗口：够算 250 日均线
_LOOKBACK_DAYS = 420


def validate_rules(rules: dict) -> None:
    """保存前校验规则合法性，抛 ExprError。"""
    for cond in rules.get("conditions", []):
        if "expr" in cond:
            validate_expr(cond["expr"])
        elif "indicator" in cond:
            if cond["indicator"] not in INDICATOR_FUNCS:
                raise ExprError(f"未知指标: {cond['indicator']}")
            if cond.get("op") not in _FUND_OPS:
                raise ExprError(f"未知比较符: {cond.get('op')}")
        elif "field" in cond:
            if cond.get("op") not in _FUND_OPS:
                raise ExprError(f"未知比较符: {cond.get('op')}")
        else:
            raise ExprError(f"条件必须包含 field / indicator / expr 之一: {cond}")


class ScreenerEngine:
    def __init__(self):
        self.store = get_bar_store()

    def run(self, db: Session, screener_id: int) -> ScreenResult:
        cfg = db.get(ScreenerConfig, screener_id)
        if cfg is None:
            raise ValueError(f"选股器 {screener_id} 不存在")
        result = ScreenResult(screener_id=cfg.id)
        try:
            rows = self._execute(cfg)
            result.symbols = rows
            result.count = len(rows)
        except Exception as e:
            logger.exception("选股器 %s 运行失败", cfg.name)
            result.error_msg = str(e)[:300]
        db.add(result)
        db.commit()
        db.refresh(result)
        return result

    def _execute(self, cfg: ScreenerConfig) -> list[dict]:
        market = Market(cfg.market)
        provider = get_provider(market)
        symbols = provider.get_universe(market, cfg.universe or {})
        if not symbols:
            return []

        conditions = (cfg.rules or {}).get("conditions", [])
        fund_conds = [c for c in conditions if "field" in c]
        tech_conds = [c for c in conditions if "field" not in c]

        # 1) 基本面过滤（一次快照，向量化）
        fundamentals = provider.get_fundamentals(market, symbols)
        survivors = list(symbols)
        if fund_conds:
            if fundamentals.empty or not set(c["field"] for c in fund_conds) & set(fundamentals.columns):
                raise ValueError("该市场暂无基本面数据，无法使用 field 条件")
            mask = pd.Series(True, index=fundamentals.index)
            for c in fund_conds:
                field, op, value = c["field"], c["op"], c["value"]
                if field not in fundamentals.columns:
                    raise ValueError(f"基本面字段不存在: {field}")
                col = pd.to_numeric(fundamentals[field], errors="coerce")
                mask &= _FUND_OPS[op](col, value).fillna(False)
            survivors = [s for s in symbols if s in mask.index and bool(mask.loc[s])]

        # 2) 技术面过滤（逐票在日线上求值）
        end = date.today().isoformat()
        start = (date.today() - timedelta(days=_LOOKBACK_DAYS)).isoformat()
        matched: list[dict] = []
        for sym in survivors:
            try:
                bars = self.store.get_bars(market, sym, start, end)
            except Exception:
                logger.warning("跳过 %s：行情获取失败", sym)
                continue
            if bars.empty or len(bars) < 30:
                continue
            ok = True
            for c in tech_conds:
                expr = c["expr"] if "expr" in c else self._indicator_to_expr(c)
                try:
                    if not eval_expr_last(expr, bars):
                        ok = False
                        break
                except ExprError:
                    raise
                except Exception:
                    logger.warning("表达式在 %s 上求值失败: %s", sym, expr)
                    ok = False
                    break
            if not ok:
                continue
            row = {"symbol": sym, "close": float(bars["close"].iloc[-1])}
            if not fundamentals.empty and sym in fundamentals.index:
                for col in fundamentals.columns:
                    val = fundamentals.loc[sym, col]
                    if pd.notna(val):
                        row[col] = val if isinstance(val, str) else float(val)
            matched.append(row)

        # 3) 排序 / 截断
        sort_by = (cfg.rules or {}).get("sort_by")
        if sort_by and matched:
            key = sort_by.get("field")
            matched.sort(key=lambda r: (r.get(key) is None, r.get(key, 0)),
                         reverse=not sort_by.get("asc", True))
        limit = (cfg.rules or {}).get("limit")
        if limit:
            matched = matched[: int(limit)]
        return matched

    @staticmethod
    def _indicator_to_expr(cond: dict) -> str:
        """{"indicator":"RSI","args":{"period":14},"op":"<","value":30} → "RSI(close, period=14) < 30" """
        args = cond.get("args", {})
        arg_str = ", ".join(f"{k}={v}" for k, v in args.items())
        call = f"{cond['indicator']}(close{', ' + arg_str if arg_str else ''})"
        return f"{call} {cond['op']} {cond['value']}"
