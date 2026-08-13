from app.screener.indicators import SMA
from app.strategy.base import Strategy, StrategyContext


class SmaCross(Strategy):
    """双均线交叉：快线上穿慢线全仓买入，下穿清仓。"""

    params = {"fast": 10, "slow": 30, "qty": 100}

    def on_bar(self, ctx: StrategyContext) -> None:
        slow_n = int(self.p["slow"])
        bars = ctx.history(slow_n + 2)
        if len(bars) < slow_n + 1:
            return
        fast = SMA(bars["close"], self.p["fast"])
        slow = SMA(bars["close"], self.p["slow"])
        golden = fast.iloc[-1] > slow.iloc[-1] and fast.iloc[-2] <= slow.iloc[-2]
        death = fast.iloc[-1] < slow.iloc[-1] and fast.iloc[-2] >= slow.iloc[-2]
        if golden and ctx.position() == 0:
            ctx.buy(self.p["qty"])
            ctx.log(f"金叉买入 {self.p['qty']}")
        elif death and ctx.position() > 0:
            ctx.close()
            ctx.log("死叉清仓")
