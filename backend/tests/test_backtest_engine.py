import pandas as pd

from app.backtest.engine import BacktestEngine
from app.backtest.metrics import compute_metrics
from app.strategy.base import Strategy


def _bars(rows: list[tuple]) -> pd.DataFrame:
    """rows: [(date, open, high, low, close)]"""
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close"])
    df["volume"] = 1000
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")


class BuyOnce(Strategy):
    """第一根 bar 买入 10 股，之后不动。"""

    params = {"qty": 10}

    def on_bar(self, ctx):
        if ctx.position() == 0 and len(ctx.history(100)) == 1:
            ctx.buy(self.p["qty"])


def test_fills_next_bar_open_no_lookahead():
    bars = {"X": _bars([
        ("2026-01-01", 10, 11, 9, 10),
        ("2026-01-02", 20, 21, 19, 20),  # 信号在第1根收盘产生 → 应按第2根 open=20 成交
        ("2026-01-03", 30, 31, 29, 30),
    ])}
    engine = BacktestEngine(BuyOnce, {}, bars, initial_cash=1000, commission_bps=0, slippage_bps=0)
    result = engine.run()
    assert len(result.trades) == 1
    assert result.trades[0].price == 20.0  # 不是第1根的 close=10（无未来函数）
    assert result.trades[0].date == "2026-01-02"


def test_commission_and_equity():
    bars = {"X": _bars([
        ("2026-01-01", 10, 10, 10, 10),
        ("2026-01-02", 10, 10, 10, 10),
        ("2026-01-03", 10, 10, 10, 12),
    ])}
    engine = BacktestEngine(BuyOnce, {}, bars, initial_cash=1000,
                            commission_bps=100, slippage_bps=0)  # 1% 佣金
    result = engine.run()
    trade = result.trades[0]
    assert trade.fee == 1.0  # 10股*10元*1%
    # 终点权益 = 1000 - 100(买入) - 1(佣金) + 10股*12 = 1019
    assert result.equity_curve[-1][1] == 1019.0


class SellAll(Strategy):
    params = {}

    def on_bar(self, ctx):
        if ctx.position() == 0 and len(ctx.history(100)) == 1:
            ctx.buy(10)
        elif ctx.position() > 0 and len(ctx.history(100)) == 3:
            ctx.close()


def test_round_trip_pnl():
    bars = {"X": _bars([
        ("2026-01-01", 10, 10, 10, 10),
        ("2026-01-02", 10, 10, 10, 10),  # buy @10
        ("2026-01-03", 10, 10, 10, 15),
        ("2026-01-04", 20, 20, 20, 20),  # sell @20
    ])}
    engine = BacktestEngine(SellAll, {}, bars, initial_cash=1000, commission_bps=0, slippage_bps=0)
    result = engine.run()
    sells = [t for t in result.trades if t.side == "sell"]
    assert len(sells) == 1
    assert sells[0].pnl == 100.0  # (20-10)*10
    metrics = compute_metrics(result)
    assert metrics["win_rate"] == 1.0
    assert metrics["trade_count"] == 2


def test_insufficient_cash_scales_down():
    class BuyHuge(Strategy):
        params = {}

        def on_bar(self, ctx):
            if ctx.position() == 0:
                ctx.buy(1000)  # 远超现金

    bars = {"X": _bars([
        ("2026-01-01", 10, 10, 10, 10),
        ("2026-01-02", 10, 10, 10, 10),
    ])}
    engine = BacktestEngine(BuyHuge, {}, bars, initial_cash=105, commission_bps=0, slippage_bps=0)
    result = engine.run()
    assert result.trades[0].qty == 10  # 105/10 → 10股
    assert engine.cash >= 0


def test_limit_order_fill():
    class LimitBuy(Strategy):
        params = {}

        def on_bar(self, ctx):
            if ctx.position() == 0 and len(ctx.history(100)) == 1:
                ctx.buy(10, limit=9.5)

    bars = {"X": _bars([
        ("2026-01-01", 10, 10, 10, 10),
        ("2026-01-02", 10, 10.5, 9.0, 10),  # low=9.0 触及 limit=9.5
    ])}
    engine = BacktestEngine(LimitBuy, {}, bars, initial_cash=1000, commission_bps=0, slippage_bps=0)
    result = engine.run()
    assert len(result.trades) == 1
    assert result.trades[0].price == 9.5


def test_limit_order_not_hit():
    class LimitBuy(Strategy):
        params = {}

        def on_bar(self, ctx):
            if ctx.position() == 0 and len(ctx.history(100)) == 1:
                ctx.buy(10, limit=8.0)

    bars = {"X": _bars([
        ("2026-01-01", 10, 10, 10, 10),
        ("2026-01-02", 10, 10.5, 9.0, 10),  # low=9.0 未触及 8.0
    ])}
    engine = BacktestEngine(LimitBuy, {}, bars, initial_cash=1000)
    result = engine.run()
    assert result.trades == []


def test_sma_cross_strategy_runs():
    """SmaCross 在构造的 V 型行情上至少完成一次买卖。"""
    from app.strategy.builtin.sma_cross import SmaCross

    rows = []
    price = 100.0
    for i in range(30):
        price -= 1  # 下跌
        rows.append((f"2026-01-{i + 1:02d}" if i < 28 else f"2026-02-{i - 27:02d}", price, price, price, price))
    for i in range(40):
        price += 2  # 反弹 → 金叉
        rows.append((pd.Timestamp("2026-02-03") + pd.Timedelta(days=i), price, price, price, price))
    bars = {"X": _bars([(str(r[0])[:10], *r[1:]) for r in rows])}
    engine = BacktestEngine(SmaCross, {"fast": 5, "slow": 15, "qty": 10}, bars, initial_cash=100000)
    result = engine.run()
    assert any(t.side == "buy" for t in result.trades)
