"""迭代11：股票内置策略扩充（MACD/布林/唐奇安/网格/定投）。"""

import numpy as np
import pandas as pd

from app.backtest.engine import BacktestEngine


def _bars(closes, start="2026-01-02", highs=None, lows=None):
    n = len(closes)
    return pd.DataFrame({
        "open": closes,
        "high": highs if highs is not None else [c * 1.005 for c in closes],
        "low": lows if lows is not None else [c * 0.995 for c in closes],
        "close": closes,
        "volume": [1000] * n,
    }, index=pd.bdate_range(start, periods=n))


def _run(cls, params, bars, cash=1_000_000):
    engine = BacktestEngine(cls, params, {"X": bars}, initial_cash=cash,
                            commission_bps=0, slippage_bps=0)
    return engine, engine.run()


# ---------- MacdTrend ----------


def test_macd_trend_buys_on_golden_cross():
    from app.strategy.builtin.macd_trend import MacdTrend

    # 长期下跌后强劲反弹 → 底部金叉（不开零轴过滤，验证基础买卖闭环）
    closes = list(np.linspace(120, 80, 60)) + list(np.linspace(80, 140, 60))
    engine, result = _run(MacdTrend, {"qty": 10, "zero_filter": False}, _bars(closes))
    buys = [t for t in result.trades if t.side == "buy"]
    assert buys, "反弹段应触发金叉买入"
    assert engine.positions.get("X", (0, 0))[0] > 0  # 上涨末期仍持仓


def test_macd_trend_zero_filter_blocks_weak_cross():
    from app.strategy.builtin.macd_trend import MacdTrend

    # 缓慢阴跌 + 微弱反弹（DIF 始终 <0）：开过滤不买，关过滤可能买
    closes = list(np.linspace(100, 70, 80)) + list(np.linspace(70, 74, 15)) \
        + list(np.linspace(74, 60, 30))
    _, filtered = _run(MacdTrend, {"qty": 10, "zero_filter": True}, _bars(closes))
    _, unfiltered = _run(MacdTrend, {"qty": 10, "zero_filter": False}, _bars(closes))
    assert len(filtered.trades) <= len(unfiltered.trades)
    assert any(t.side == "buy" for t in unfiltered.trades)
    assert not any(t.side == "buy" for t in filtered.trades)


# ---------- BollingerReversion ----------


def test_bollinger_reversion_cycle():
    from app.strategy.builtin.bollinger_reversion import BollingerReversion

    # 横盘 → 急跌破下轨 → 修复回中轨
    closes = [100.0] * 30 + [90.0] + list(np.linspace(90, 101, 15))
    _, result = _run(BollingerReversion, {"period": 20, "qty": 10}, _bars(closes))
    sides = [t.side for t in result.trades]
    assert sides[:2] == ["buy", "sell"]  # 完整一买一卖
    sells = [t for t in result.trades if t.side == "sell"]
    assert sells[0].pnl > 0  # 低买中轨卖应盈利


# ---------- DonchianBreakout ----------


def test_donchian_breakout_and_exit():
    from app.strategy.builtin.donchian_breakout import DonchianBreakout

    # 横盘 25 天 → 放量突破新高 → 回落跌破 10 日低点
    closes = [100.0] * 25 + [103.0, 106.0, 109.0] + list(np.linspace(109, 92, 12))
    _, result = _run(DonchianBreakout, {"entry_n": 20, "exit_n": 10, "qty": 10},
                     _bars(closes))
    assert [t.side for t in result.trades][:2] == ["buy", "sell"]
    buy = result.trades[0]
    assert buy.price > 100.0  # 在突破段成交


def test_donchian_no_lookahead_on_flat():
    from app.strategy.builtin.donchian_breakout import DonchianBreakout

    closes = [100.0] * 60  # 纯横盘不该有任何交易
    _, result = _run(DonchianBreakout, {}, _bars(closes))
    assert result.trades == []


# ---------- GridTrading ----------


def test_grid_trading_adds_and_reduces():
    from app.strategy.builtin.grid_trading import GridTrading

    # 中枢 ~100：跌到 91（约 2-3 格加仓）再涨回 95.5（1 格，部分减仓）
    closes = [100.0] * 60 + [91.0] * 3 + [95.5] * 3
    _, result = _run(GridTrading,
                     {"grid_pct": 3, "grids": 5, "qty_per_grid": 100, "center_period": 60},
                     _bars(closes))
    buys = [t for t in result.trades if t.side == "buy"]
    sells = [t for t in result.trades if t.side == "sell"]
    assert buys and sells
    assert sum(t.qty for t in buys) > sum(t.qty for t in sells)
    # 减仓段盈利（91 买 97 卖）
    assert all(t.pnl > 0 for t in sells if t.pnl is not None)


def test_grid_trading_flat_market_no_churn():
    from app.strategy.builtin.grid_trading import GridTrading

    closes = [100.0] * 80  # 价格贴着中枢 → 0 格 → 不交易
    _, result = _run(GridTrading, {}, _bars(closes))
    assert result.trades == []


# ---------- DcaInvest ----------


def test_dca_buys_first_trading_day_each_month():
    from app.strategy.builtin.dca_invest import DcaInvest

    closes = [100.0] * 70  # 覆盖 2026-01 ~ 2026-04（工作日）
    bars = _bars(closes, start="2026-01-02")
    _, result = _run(DcaInvest, {"amount": 10000}, bars)
    buys = [t for t in result.trades if t.side == "buy"]
    # 首月第一天没有"上月参照"不买；随后每月首个交易日各一笔
    months = {t.date[:7] for t in buys}
    assert len(buys) == len(months) >= 2
    assert all(t.qty == 100 for t in buys)  # 10000/100 = 100 股
    assert not any(t.side == "sell" for t in result.trades)


def test_dca_fixed_qty_mode():
    from app.strategy.builtin.dca_invest import DcaInvest

    bars = _bars([50.0] * 50, start="2026-01-02")
    _, result = _run(DcaInvest, {"amount": 0, "qty": 7}, bars)
    buys = [t for t in result.trades if t.side == "buy"]
    assert buys and all(t.qty == 7 for t in buys)


# ---------- 注册表 ----------


def test_registry_has_all_stock_strategies(seeded):
    from app.strategy.registry import list_strategies

    items = {s["class_name"]: s["kind"] for s in list_strategies()}
    for name in ("SmaCross", "MacdTrend", "RsiReversion", "BollingerReversion",
                 "DonchianBreakout", "GridTrading", "DcaInvest"):
        assert items[name] == "single"
    assert items["MomentumRotation"] == "portfolio"
    assert items["CoveredCall"] == items["CashSecuredPut"] == items["WheelStrategy"] == "option"
