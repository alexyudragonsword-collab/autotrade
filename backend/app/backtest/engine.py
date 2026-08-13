"""轻量 bar 级事件驱动回测引擎。

撮合规则（防未来函数）：
- on_bar 在第 i 根 bar 收盘后调用，产生的订单在第 i+1 根 bar 撮合
- 市价单按 i+1 根 open 成交（加滑点）；限价单当且仅当 i+1 根价格区间触及 limit 时成交
- 佣金/滑点按 bps 计
"""

from dataclasses import dataclass, field

import pandas as pd

from app.strategy.base import Strategy, StrategyContext


@dataclass
class BtOrder:
    symbol: str
    side: str  # buy | sell
    qty: float
    limit: float | None = None


@dataclass
class BtTrade:
    date: str
    symbol: str
    side: str
    qty: float
    price: float
    fee: float
    pnl: float | None = None  # 卖出时记录已实现盈亏


@dataclass
class BtResult:
    equity_curve: list = field(default_factory=list)  # [[date, equity], ...]
    trades: list[BtTrade] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    initial_cash: float = 0.0
    final_equity: float = 0.0


class _BacktestContext(StrategyContext):
    def __init__(self, engine: "BacktestEngine", symbol: str):
        self._engine = engine
        self.symbol = symbol
        self._i = 0  # 当前 bar 序号

    def history(self, n: int) -> pd.DataFrame:
        bars = self._engine.bars[self.symbol]
        lo = max(0, self._i + 1 - n)
        return bars.iloc[lo: self._i + 1]

    def position(self) -> float:
        return self._engine.positions.get(self.symbol, (0.0, 0.0))[0]

    def buy(self, qty: float, limit: float | None = None) -> None:
        if qty > 0:
            self._engine.pending.append(BtOrder(self.symbol, "buy", qty, limit))

    def sell(self, qty: float, limit: float | None = None) -> None:
        if qty > 0:
            self._engine.pending.append(BtOrder(self.symbol, "sell", qty, limit))

    def log(self, msg: str) -> None:
        date = self._engine.calendar[self._i].date().isoformat()
        self._engine.result.logs.append(f"[{date}] {self.symbol}: {msg}")


class BacktestEngine:
    def __init__(self, strategy_cls: type[Strategy], params: dict,
                 bars: dict[str, pd.DataFrame], initial_cash: float = 100_000.0,
                 commission_bps: float = 3.0, slippage_bps: float = 1.0,
                 progress_cb=None):
        self.strategy_cls = strategy_cls
        self.params = params or {}
        self.bars = {s: df.sort_index() for s, df in bars.items() if not df.empty}
        self.cash = initial_cash
        self.commission = commission_bps / 10_000
        self.slippage = slippage_bps / 10_000
        # symbol -> (qty, avg_cost)
        self.positions: dict[str, tuple[float, float]] = {}
        self.pending: list[BtOrder] = []
        self.result = BtResult(initial_cash=initial_cash)
        self.progress_cb = progress_cb
        # 统一交易日历（所有标的日期并集）
        idx = pd.DatetimeIndex([])
        for df in self.bars.values():
            idx = idx.union(df.index)
        self.calendar = idx.sort_values()

    def run(self) -> BtResult:
        if not self.bars or len(self.calendar) == 0:
            self.result.final_equity = self.cash
            return self.result
        contexts = {s: _BacktestContext(self, s) for s in self.bars}
        strategies = {s: self.strategy_cls(**self.params) for s in self.bars}
        for s, ctx in contexts.items():
            strategies[s].on_start(ctx)

        total = len(self.calendar)
        for i, ts in enumerate(self.calendar):
            # 1) 撮合上一根 bar 收盘后产生的订单（用今日行情）
            self._match_pending(ts)
            # 2) 逐标的调 on_bar（仅当该标的今日有数据）
            for sym, df in self.bars.items():
                if ts not in df.index:
                    continue
                ctx = contexts[sym]
                ctx._i = df.index.get_loc(ts)
                strategies[sym].on_bar(ctx)
            # 3) 收盘计权益
            self.result.equity_curve.append([ts.date().isoformat(), round(self._equity(ts), 2)])
            if self.progress_cb and (i % 50 == 0 or i == total - 1):
                self.progress_cb((i + 1) / total)

        for s, ctx in contexts.items():
            strategies[s].on_stop(ctx)
        self.result.final_equity = self.result.equity_curve[-1][1] if self.result.equity_curve else self.cash
        return self.result

    # ---------- 内部 ----------

    def _match_pending(self, ts: pd.Timestamp) -> None:
        still_pending: list[BtOrder] = []
        for order in self.pending:
            df = self.bars[order.symbol]
            if ts not in df.index:
                still_pending.append(order)  # 该标的今日停牌，顺延
                continue
            bar = df.loc[ts]
            price = self._fill_price(order, bar)
            if price is None:
                continue  # 限价未触及，本引擎按"当日有效"处理，直接丢弃
            self._execute(order, price, ts)
        self.pending = still_pending

    def _fill_price(self, order: BtOrder, bar: pd.Series) -> float | None:
        if order.limit is None:
            slip = 1 + self.slippage if order.side == "buy" else 1 - self.slippage
            return float(bar["open"]) * slip
        # 限价单：买单 limit >= low 可成交，卖单 limit <= high 可成交
        if order.side == "buy" and bar["low"] <= order.limit:
            return min(order.limit, float(bar["open"]))
        if order.side == "sell" and bar["high"] >= order.limit:
            return max(order.limit, float(bar["open"]))
        return None

    def _execute(self, order: BtOrder, price: float, ts: pd.Timestamp) -> None:
        qty, avg = self.positions.get(order.symbol, (0.0, 0.0))
        fee = price * order.qty * self.commission
        date = ts.date().isoformat()
        if order.side == "buy":
            cost = price * order.qty + fee
            if cost > self.cash:  # 现金不足按可买数量缩量
                order.qty = int(self.cash / (price * (1 + self.commission)))
                if order.qty <= 0:
                    return
                fee = price * order.qty * self.commission
                cost = price * order.qty + fee
            new_qty = qty + order.qty
            new_avg = (qty * avg + price * order.qty) / new_qty if new_qty else 0.0
            self.positions[order.symbol] = (new_qty, new_avg)
            self.cash -= cost
            self.result.trades.append(BtTrade(date, order.symbol, "buy", order.qty, round(price, 4), round(fee, 4)))
        else:
            sell_qty = min(order.qty, qty)
            if sell_qty <= 0:
                return
            proceeds = price * sell_qty - fee
            pnl = (price - avg) * sell_qty - fee
            remaining = qty - sell_qty
            self.positions[order.symbol] = (remaining, avg if remaining else 0.0)
            self.cash += proceeds
            self.result.trades.append(
                BtTrade(date, order.symbol, "sell", sell_qty, round(price, 4), round(fee, 4), round(pnl, 2))
            )

    def _equity(self, ts: pd.Timestamp) -> float:
        total = self.cash
        for sym, (qty, _) in self.positions.items():
            if qty == 0:
                continue
            df = self.bars[sym]
            visible = df.loc[df.index <= ts]
            if not visible.empty:
                total += qty * float(visible["close"].iloc[-1])
        return total
