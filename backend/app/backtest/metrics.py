"""回测绩效指标。"""

import numpy as np

from app.backtest.engine import BtResult

TRADING_DAYS = 252


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
