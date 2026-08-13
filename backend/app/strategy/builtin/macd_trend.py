from app.screener.indicators import EMA
from app.strategy.base import Strategy, StrategyContext


class MacdTrend(Strategy):
    """MACD 趋势：柱由负转正（金叉）买入，转负（死叉）清仓；可选 DIF>0 强趋势过滤。"""

    params = {"fast": 12, "slow": 26, "signal": 9, "qty": 100, "zero_filter": True}

    def on_bar(self, ctx: StrategyContext) -> None:
        need = int(self.p["slow"]) + int(self.p["signal"]) + 5
        bars = ctx.history(need * 2)
        if len(bars) < need:
            return
        close = bars["close"]
        dif = EMA(close, self.p["fast"]) - EMA(close, self.p["slow"])
        dea = dif.ewm(span=int(self.p["signal"]), adjust=False).mean()
        hist = dif - dea

        golden = hist.iloc[-1] > 0 >= hist.iloc[-2]
        death = hist.iloc[-1] < 0 <= hist.iloc[-2]
        trend_ok = (not self.p["zero_filter"]) or dif.iloc[-1] > 0

        if golden and trend_ok and ctx.position() == 0:
            ctx.buy(self.p["qty"])
            ctx.log(f"MACD 金叉买入（DIF={dif.iloc[-1]:.3f}）")
        elif death and ctx.position() > 0:
            ctx.close()
            ctx.log("MACD 死叉清仓")
