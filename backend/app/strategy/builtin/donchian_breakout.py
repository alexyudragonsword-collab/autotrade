from app.screener.indicators import HIGHEST, LOWEST
from app.strategy.base import Strategy, StrategyContext


class DonchianBreakout(Strategy):
    """唐奇安通道突破（海龟法则）：收盘突破前 N 日最高买入，跌破前 M 日最低清仓。"""

    params = {"entry_n": 20, "exit_n": 10, "qty": 100}

    def on_bar(self, ctx: StrategyContext) -> None:
        n = int(self.p["entry_n"])
        bars = ctx.history(n + 5)
        if len(bars) < n + 2:
            return
        # 用「前一日为止」的通道，避免把当日纳入自身突破判定
        upper = HIGHEST(bars["high"], n).shift(1)
        lower = LOWEST(bars["low"], int(self.p["exit_n"])).shift(1)
        close = bars["close"].iloc[-1]

        if close > upper.iloc[-1] and ctx.position() == 0:
            ctx.buy(self.p["qty"])
            ctx.log(f"突破 {n} 日新高 {upper.iloc[-1]:.2f} 买入")
        elif close < lower.iloc[-1] and ctx.position() > 0:
            ctx.close()
            ctx.log(f"跌破 {self.p['exit_n']} 日新低 {lower.iloc[-1]:.2f} 清仓")
