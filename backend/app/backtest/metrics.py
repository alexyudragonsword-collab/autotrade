"""回测绩效指标。"""

import numpy as np
import pandas as pd

from app.backtest.engine import BtResult

TRADING_DAYS = 252


def benchmark_curve(bars: dict[str, pd.DataFrame], calendar: pd.DatetimeIndex,
                    initial_cash: float) -> list[float]:
    """等权买入持有基准：期初按首个可见收盘价等权买入，此后不动。"""
    n = len(bars)
    if n == 0 or len(calendar) == 0:
        return []
    alloc = initial_cash / n
    shares = {sym: alloc / float(df["close"].iloc[0]) for sym, df in bars.items()}
    curve = []
    for ts in calendar:
        total = 0.0
        for sym, df in bars.items():
            visible = df.loc[df.index <= ts]
            if visible.empty:
                total += alloc  # 尚无行情按现金计
            else:
                total += shares[sym] * float(visible["close"].iloc[-1])
        curve.append(round(total, 2))
    return curve


def monthly_returns(equity_curve: list) -> list[dict]:
    """[[date, equity, ...], ...] → [{"month": "2026-01", "ret": 0.023}, ...]"""
    if len(equity_curve) < 2:
        return []
    s = pd.Series([p[1] for p in equity_curve],
                  index=pd.to_datetime([p[0] for p in equity_curve]))
    month_end = s.resample("ME").last()
    rets = month_end.pct_change()
    # 首月相对期初
    rets.iloc[0] = month_end.iloc[0] / s.iloc[0] - 1
    return [{"month": ts.strftime("%Y-%m"), "ret": round(float(r), 4)}
            for ts, r in rets.items() if pd.notna(r)]


def compute_metrics(result: BtResult) -> dict:
    curve = result.equity_curve
    if len(curve) < 2:
        return {"total_return": 0.0, "annual_return": 0.0, "sharpe": 0.0,
                "max_drawdown": 0.0, "win_rate": 0.0, "trade_count": len(result.trades),
                "final_equity": result.final_equity}
    equity = np.array([e for _, e in curve], dtype=float)
    returns = np.diff(equity) / equity[:-1]

    total_return = equity[-1] / equity[0] - 1
    years = max(len(equity) / TRADING_DAYS, 1e-9)
    annual_return = (equity[-1] / equity[0]) ** (1 / years) - 1
    sharpe = 0.0
    if returns.std() > 1e-12:
        sharpe = returns.mean() / returns.std() * np.sqrt(TRADING_DAYS)
    peak = np.maximum.accumulate(equity)
    max_drawdown = float(((equity - peak) / peak).min())

    closed = [t for t in result.trades if t.pnl is not None]
    wins = sum(1 for t in closed if t.pnl > 0)
    win_rate = wins / len(closed) if closed else 0.0

    return {
        "total_return": round(float(total_return), 4),
        "annual_return": round(float(annual_return), 4),
        "sharpe": round(float(sharpe), 3),
        "max_drawdown": round(max_drawdown, 4),
        "win_rate": round(win_rate, 4),
        "trade_count": len(result.trades),
        "closed_trade_count": len(closed),
        "final_equity": round(float(equity[-1]), 2),
    }
