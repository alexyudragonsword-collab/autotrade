"""组合策略抽象：跨标的视角 + 周期性再平衡。

与单标的 Strategy 不同，PortfolioStrategy 在每个再平衡日拿到全部标的的
上下文，一次性给出目标市值（order_target_value），由引擎/实盘层换算成
买卖差额。回测与实盘共用同一份 on_rebalance 代码。
"""

from abc import ABC, abstractmethod

import pandas as pd


class PortfolioContext(ABC):
    symbols: list[str] = []

    @abstractmethod
    def history(self, symbol: str, n: int) -> pd.DataFrame:
        """当前时点（含）往前 n 根 K 线。"""

    @abstractmethod
    def position(self, symbol: str) -> float: ...

    @abstractmethod
    def price(self, symbol: str) -> float:
        """当前时点最新收盘价。"""

    @abstractmethod
    def equity(self) -> float:
        """总权益 = 现金 + 全部持仓市值。"""

    @abstractmethod
    def order_target_value(self, symbol: str, value: float) -> None:
        """调仓到目标市值（0 = 清仓）。差额由引擎换算成市价买卖单。"""

    def log(self, msg: str) -> None:  # noqa: B027
        pass


class PortfolioStrategy(ABC):
    """周期性组合再平衡策略基类。

    params 中的 rebalance 控制频率：daily / weekly / monthly。
    """

    params: dict = {"rebalance": "monthly"}

    def __init__(self, **overrides):
        self.p = {**self.params, **overrides}

    @abstractmethod
    def on_rebalance(self, ctx: PortfolioContext) -> None: ...


def is_rebalance_day(prev: pd.Timestamp | None, current: pd.Timestamp, freq: str) -> bool:
    """判断 current 是否为新周期首个交易日（prev 为上一交易日，None=首日）。"""
    if prev is None:
        return True
    if freq == "daily":
        return True
    if freq == "weekly":
        return current.isocalendar()[:2] != prev.isocalendar()[:2]
    # monthly（默认）
    return (current.year, current.month) != (prev.year, prev.month)


class MomentumRotation(PortfolioStrategy):
    """动量轮动：按过去 lookback 根 K 线涨幅排序，等权持有前 top_k 名，其余清仓。"""

    params = {"rebalance": "monthly", "lookback": 120, "top_k": 2, "reserve": 0.02}

    def on_rebalance(self, ctx: PortfolioContext) -> None:
        lookback = int(self.p["lookback"])
        scores: dict[str, float] = {}
        for sym in ctx.symbols:
            bars = ctx.history(sym, lookback + 1)
            if len(bars) < lookback // 2:  # 数据不足一半不参与排名
                continue
            first, last = float(bars["close"].iloc[0]), float(bars["close"].iloc[-1])
            if first > 0:
                scores[sym] = last / first - 1

        top_k = max(1, int(self.p["top_k"]))
        winners = [s for s, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]]
        # 预留 reserve 比例现金付手续费，避免满仓下单因现金不足缩量
        target = ctx.equity() * (1 - float(self.p["reserve"])) / top_k if winners else 0.0

        for sym in ctx.symbols:
            if sym in winners:
                ctx.order_target_value(sym, target)
            elif ctx.position(sym) > 0:
                ctx.order_target_value(sym, 0.0)
        if winners:
            ctx.log(f"再平衡：持有 {winners}，每只目标市值 {target:,.0f}")
