"""自定义策略：编译、校验、热加载。

代码在预置命名空间中 exec（pd/np/指标函数/策略基类已就绪），
取其中定义的 Strategy 或 PortfolioStrategy 子类。保存前用合成行情
试跑一遍回测，把运行期错误挡在保存阶段。

安全说明：exec 即完整 Python 权限。本系统为自托管单用户部署，
编辑器仅登录管理员可用；请勿粘贴来源不明的策略代码。
"""

import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import CustomStrategy
from app.screener import indicators as _indicators
from app.strategy.base import Strategy, StrategyContext
from app.strategy.options import OptionStrategy, OptionStrategyContext
from app.strategy.portfolio import PortfolioContext, PortfolioStrategy

logger = logging.getLogger(__name__)


class StrategyCodeError(ValueError):
    pass


def _exec_namespace() -> dict:
    ns = {
        "pd": pd, "np": np,
        "Strategy": Strategy, "StrategyContext": StrategyContext,
        "PortfolioStrategy": PortfolioStrategy, "PortfolioContext": PortfolioContext,
        "OptionStrategy": OptionStrategy, "OptionStrategyContext": OptionStrategyContext,
    }
    for name, fn in _indicators.INDICATOR_FUNCS.items():
        ns[name] = fn
    return ns


def compile_strategy_code(code: str) -> type:
    """编译代码并返回其中定义的策略类（恰好一个）。"""
    if len(code) > 100_000:
        raise StrategyCodeError("代码过长")
    ns = _exec_namespace()
    baseline = set(ns)
    try:
        exec(compile(code, "<custom_strategy>", "exec"), ns)  # noqa: S102
    except Exception as e:
        raise StrategyCodeError(f"代码执行失败: {type(e).__name__}: {e}")

    candidates = [
        obj for name, obj in ns.items()
        if name not in baseline and isinstance(obj, type)
        and issubclass(obj, (Strategy, PortfolioStrategy, OptionStrategy))
        and obj not in (Strategy, PortfolioStrategy, OptionStrategy)
    ]
    if not candidates:
        raise StrategyCodeError("代码中必须定义一个 Strategy / PortfolioStrategy / OptionStrategy 子类")
    if len(candidates) > 1:
        raise StrategyCodeError(f"只允许定义一个策略类，发现 {len(candidates)} 个: "
                                f"{[c.__name__ for c in candidates]}")
    cls = candidates[0]
    if not isinstance(getattr(cls, "params", {}), dict):
        raise StrategyCodeError("params 必须是 dict")
    return cls


def _synthetic_bars(n: int = 60) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, n)))
    return pd.DataFrame({
        "open": close, "high": close * 1.01, "low": close * 0.99,
        "close": close, "volume": rng.integers(1000, 100000, n),
    }, index=pd.bdate_range("2024-01-01", periods=n))


def validate_strategy_class(cls: type) -> dict:
    """合成行情上试跑一遍回测，返回 {'ok', 'kind', 'trades'} 或抛 StrategyCodeError。"""
    from app.backtest.engine import BacktestEngine

    if issubclass(cls, OptionStrategy):
        # 期权策略无法回测（无历史数据源），仅校验结构
        if not callable(getattr(cls, "on_run", None)):
            raise StrategyCodeError("期权策略必须实现 async on_run(self, ctx)")
        return {"ok": True, "kind": "option", "trades": 0}

    bars = {"US.TEST1": _synthetic_bars(), "US.TEST2": _synthetic_bars()}
    try:
        engine = BacktestEngine(cls, {}, bars, initial_cash=100000)
        result = engine.run()
    except Exception as e:
        raise StrategyCodeError(f"试跑失败: {type(e).__name__}: {e}")
    return {
        "ok": True,
        "kind": "portfolio" if issubclass(cls, PortfolioStrategy) else "single",
        "trades": len(result.trades),
    }


# ---------- 热加载缓存 ----------

_cache: dict[str, tuple[datetime, type]] = {}


def load_custom_strategy(class_name: str) -> type | None:
    """从 DB 加载自定义策略类；updated_at 变化时重新编译。"""
    db = SessionLocal()
    try:
        row = db.scalar(select(CustomStrategy).where(CustomStrategy.class_name == class_name,
                                                     CustomStrategy.enabled))
    finally:
        db.close()
    if row is None:
        _cache.pop(class_name, None)
        return None
    stamp = row.updated_at or datetime.now(timezone.utc)
    cached = _cache.get(class_name)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        cls = compile_strategy_code(row.code)
    except StrategyCodeError:
        logger.exception("自定义策略 %s 编译失败", class_name)
        return None
    _cache[class_name] = (stamp, cls)
    return cls


def list_custom_strategies() -> list[dict]:
    db = SessionLocal()
    try:
        rows = db.scalars(select(CustomStrategy).where(CustomStrategy.enabled)).all()
    finally:
        db.close()
    out = []
    for row in rows:
        cls = load_custom_strategy(row.class_name)
        if cls is None:
            continue
        out.append({
            "class_name": row.class_name,
            "params": getattr(cls, "params", {}),
            "doc": (cls.__doc__ or "").strip().split("\n")[0],
            "kind": ("option" if issubclass(cls, OptionStrategy)
                     else "portfolio" if issubclass(cls, PortfolioStrategy) else "single"),
            "custom": True,
        })
    return out


DEFAULT_TEMPLATE = '''class MyStrategy(Strategy):
    """示例：收盘价上穿 20 日均线买入，下穿卖出。"""

    params = {"period": 20, "qty": 100}

    def on_bar(self, ctx):
        n = int(self.p["period"])
        bars = ctx.history(n + 2)
        if len(bars) < n + 1:
            return
        ma = SMA(bars["close"], n)
        above = bars["close"].iloc[-1] > ma.iloc[-1]
        was_below = bars["close"].iloc[-2] <= ma.iloc[-2]
        if above and was_below and ctx.position() == 0:
            ctx.buy(self.p["qty"])
        elif not above and ctx.position() > 0:
            ctx.close()
'''
