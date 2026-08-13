"""迭代4：多周期支持与组合再平衡策略。"""

import numpy as np
import pandas as pd
import pytest

from app.backtest.engine import BacktestEngine
from app.strategy.portfolio import MomentumRotation, is_rebalance_day


def _bars(closes, start="2026-01-01", freq="B"):
    n = len(closes)
    return pd.DataFrame({
        "open": closes, "high": [c * 1.005 for c in closes],
        "low": [c * 0.995 for c in closes], "close": closes,
        "volume": [1000] * n,
    }, index=pd.date_range(start, periods=n, freq=freq))


# ---------- 多周期 ----------


def test_barstore_interval_paths(tmp_path):
    from app.data.store import BarStore

    store = BarStore(base_dir=tmp_path)
    daily = _bars([1.0, 2.0, 3.0])
    minute = _bars([1.0] * 10, freq="15min")
    store.save("US.X", daily)
    store.save("US.X", minute, interval="15m")
    assert (tmp_path / "US_X.parquet").exists()
    assert (tmp_path / "US_X_15m.parquet").exists()
    assert len(store.load("US.X")) == 3
    assert len(store.load("US.X", "15m")) == 10
    # 分钟线按日期过滤要包含当日盘中时间
    got = store.get_bars(None, "US.X", "2026-01-01", "2026-01-01", refresh=False, interval="15m")
    assert len(got) == 10


def test_ts_label():
    from app.backtest.engine import ts_label

    assert ts_label(pd.Timestamp("2026-01-05")) == "2026-01-05"
    assert ts_label(pd.Timestamp("2026-01-05 10:30:00")) == "2026-01-05 10:30"


def test_minute_backtest_next_bar_fill():
    """分钟线回测同样遵循下一根 bar 开盘撮合。"""
    from app.strategy.base import Strategy

    class BuyOnce(Strategy):
        params = {}

        def on_bar(self, ctx):
            if ctx.position() == 0 and len(ctx.history(100)) == 1:
                ctx.buy(10)

    bars = {"X": _bars([10.0, 20.0, 30.0], start="2026-01-05 09:30", freq="15min")}
    engine = BacktestEngine(BuyOnce, {}, bars, initial_cash=1000,
                            commission_bps=0, slippage_bps=0)
    result = engine.run()
    assert result.trades[0].price == 20.0
    assert result.trades[0].date == "2026-01-05 09:45"
    assert result.equity_curve[0][0] == "2026-01-05 09:30"


# ---------- 再平衡日判定 ----------


def test_is_rebalance_day():
    jan29 = pd.Timestamp("2026-01-29")
    jan30 = pd.Timestamp("2026-01-30")
    feb02 = pd.Timestamp("2026-02-02")
    assert is_rebalance_day(None, jan29, "monthly")
    assert not is_rebalance_day(jan29, jan30, "monthly")
    assert is_rebalance_day(jan30, feb02, "monthly")
    # weekly：1/30 周五 → 2/2 周一为新周
    assert is_rebalance_day(jan30, feb02, "weekly")
    assert not is_rebalance_day(jan29, jan30, "weekly")
    assert is_rebalance_day(jan29, jan30, "daily")


# ---------- 组合回测 ----------


def test_momentum_rotation_backtest():
    """两只票一涨一跌：策略应持有涨的、清仓/不持有跌的。"""
    n = 140
    up = list(np.linspace(100, 200, n))
    down = list(np.linspace(100, 50, n))
    bars = {"UP": _bars(up), "DOWN": _bars(down)}
    engine = BacktestEngine(MomentumRotation,
                            {"rebalance": "monthly", "lookback": 60, "top_k": 1},
                            bars, initial_cash=100000, commission_bps=0, slippage_bps=0)
    result = engine.run()

    assert result.trades, "应该发生调仓交易"
    up_qty = engine.positions.get("UP", (0, 0))[0]
    down_qty = engine.positions.get("DOWN", (0, 0))[0]
    assert up_qty > 0
    assert down_qty == 0
    # 只买过 UP，从未买 DOWN
    assert all(t.symbol == "UP" for t in result.trades if t.side == "buy")
    # 持有强势票收益应为正
    assert result.final_equity > 100000


def test_momentum_rotation_rebalances_monthly_only():
    n = 100
    bars = {"A": _bars(list(np.linspace(100, 150, n))),
            "B": _bars(list(np.linspace(100, 140, n)))}
    engine = BacktestEngine(MomentumRotation,
                            {"rebalance": "monthly", "lookback": 30, "top_k": 1},
                            bars, initial_cash=50000, commission_bps=0, slippage_bps=0)
    result = engine.run()
    trade_months = {t.date[:7] for t in result.trades}
    months_in_calendar = {ts.strftime("%Y-%m") for ts in engine.calendar}
    # 调仓只发生在部分月份首日附近，交易月数不超过日历月数
    assert len(trade_months) <= len(months_in_calendar)
    # 非再平衡日不应有新订单：交易日期都在每月最早两个交易日内
    by_month = {}
    for ts in engine.calendar:
        by_month.setdefault(ts.strftime("%Y-%m"), []).append(ts)
    allowed = set()
    for month, days in by_month.items():
        for d in sorted(days)[:2]:  # 再平衡日 + 次日撮合
            allowed.add(d.date().isoformat())
    assert all(t.date in allowed for t in result.trades)


def test_registry_lists_portfolio_kind():
    from app.strategy.registry import get_strategy_class, list_strategies

    items = {s["class_name"]: s for s in list_strategies()}
    assert items["MomentumRotation"]["kind"] == "portfolio"
    assert items["SmaCross"]["kind"] == "single"
    assert get_strategy_class("MomentumRotation") is MomentumRotation


# ---------- 组合策略实盘信号 ----------


@pytest.fixture
async def live_env(seeded, monkeypatch, tmp_path):
    import app.brokers.manager as manager_mod
    import app.execution.order_manager as om_mod
    import app.notify.dispatcher as dispatcher_mod
    import app.risk.engine as risk_mod
    import app.signals.pipeline as pipeline_mod
    import app.strategy.live as live_mod
    from datetime import datetime, timezone

    from app.brokers.manager import BrokerManager
    from app.brokers.paper import PaperBroker
    from app.data.store import BarStore
    from app.execution.order_manager import OrderManager
    from app.risk.engine import RiskEngine
    from app.risk.rules import default_rules
    from app.signals.pipeline import SignalPipeline

    async def quote(symbol):
        return 100.0

    broker = PaperBroker(quote_fn=quote, fee_bps=0)
    await broker.connect()
    manager = BrokerManager()
    manager.register(broker)
    om = OrderManager(manager)
    om.attach_callbacks()

    class MockDispatcher:
        async def emit(self, event):
            pass

    monkeypatch.setattr(manager_mod, "_manager", manager)
    monkeypatch.setattr(om_mod, "_order_manager", om)
    monkeypatch.setattr(dispatcher_mod, "_dispatcher", MockDispatcher())
    monkeypatch.setattr(risk_mod, "_engine", RiskEngine(rules=default_rules(
        now_fn=lambda: datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc))))
    monkeypatch.setattr(pipeline_mod, "_pipeline", SignalPipeline())

    store = BarStore(base_dir=tmp_path)
    n = 140
    store.save("US.UPP", _bars(list(np.linspace(100, 200, n))))
    store.save("US.DWN", _bars(list(np.linspace(100, 50, n))))

    class FrozenStore(BarStore):
        def get_bars(self, market, symbol, start, end, refresh=True, interval="1d"):
            return super().get_bars(market, symbol, start, end, refresh=False, interval=interval)

    monkeypatch.setattr(live_mod, "get_bar_store", lambda: FrozenStore(base_dir=tmp_path))
    return {"broker": broker}


async def test_portfolio_live_generates_rebalance_signals(live_env, seeded):
    import asyncio

    from sqlalchemy import select

    from app.db.models import Order, RiskConfig, StrategyConfig
    from app.strategy.live import run_strategy_live

    # 放宽风控限额（默认单笔上限 5 万，目标市值接近 100 万）
    risk = seeded.get(RiskConfig, 1)
    risk.max_order_value = 10_000_000
    risk.max_position_value_per_symbol = 10_000_000
    risk.max_total_exposure = 10_000_000
    seeded.commit()

    seeded.add(StrategyConfig(
        name="rot", class_name="MomentumRotation",
        params={"rebalance": "monthly", "lookback": 60, "top_k": 1},
        enabled=True, mode="live", broker="paper",
        symbols=["US.UPP", "US.DWN"]))
    seeded.commit()
    cfg_id = seeded.scalar(select(StrategyConfig.id).where(StrategyConfig.name == "rot"))

    summary = await run_strategy_live(cfg_id)
    await asyncio.sleep(0.25)
    assert summary["errors"] == []
    assert summary["signals"] == 1  # 空仓 → 只买入 UPP

    orders = seeded.scalars(select(Order)).all()
    assert len(orders) == 1
    assert orders[0].symbol == "US.UPP"
    assert orders[0].side == "buy"
    assert orders[0].status == "filled"
