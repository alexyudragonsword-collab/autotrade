from app.screener.indicators import SMA, STD
from app.strategy.base import Strategy, StrategyContext


class BollingerReversion(Strategy):
    """布林带均值回归：收盘跌破下轨买入，回到中轨（均线）清仓。适合震荡市。"""

    params = {"period": 20, "num_std": 2.0, "qty": 100}

    def on_bar(self, ctx: StrategyContext) -> None:
        n = int(self.p["period"])
        bars = ctx.history(n + 5)
        if len(bars) < n + 1:
            return
        close = bars["close"]
        mid = SMA(close, n)
        std = STD(close, n)
        lower = mid.iloc[-1] - float(self.p["num_std"]) * std.iloc[-1]

        if close.iloc[-1] < lower and ctx.position() == 0:
            ctx.buy(self.p["qty"])
            ctx.log(f"跌破下轨 {lower:.2f} 买入")
        elif close.iloc[-1] >= mid.iloc[-1] and ctx.position() > 0:
            ctx.close()
            ctx.log(f"回归中轨 {mid.iloc[-1]:.2f} 清仓")
