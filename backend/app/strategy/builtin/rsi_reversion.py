from app.screener.indicators import RSI
from app.strategy.base import Strategy, StrategyContext


class RsiReversion(Strategy):
    """RSI 超卖买入、超买卖出的均值回归。"""

    params = {"period": 14, "oversold": 30, "overbought": 70, "qty": 100}

    def on_bar(self, ctx: StrategyContext) -> None:
        bars = ctx.history(int(self.p["period"]) * 3 + 5)
        if len(bars) < self.p["period"] + 2:
            return
        rsi = RSI(bars["close"], self.p["period"]).iloc[-1]
        if rsi < self.p["oversold"] and ctx.position() == 0:
            ctx.buy(self.p["qty"])
            ctx.log(f"RSI={rsi:.1f} 超卖买入")
        elif rsi > self.p["overbought"] and ctx.position() > 0:
            ctx.close()
            ctx.log(f"RSI={rsi:.1f} 超买清仓")
