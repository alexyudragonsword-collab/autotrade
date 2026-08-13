from app.strategy.base import Strategy, StrategyContext


class DcaInvest(Strategy):
    """定投：每月第一个交易日按固定金额（或固定数量）买入，长期摊薄成本。

    amount > 0 时按金额换算股数（向下取整），否则买入固定 qty 股。
    只买不卖——退出由人工或持仓守护的止盈决定。
    """

    params = {"amount": 10000.0, "qty": 0, "min_shares": 1}

    def on_bar(self, ctx: StrategyContext) -> None:
        bars = ctx.history(2)
        if len(bars) < 2:
            return
        cur, prev = bars.index[-1], bars.index[-2]
        if (cur.year, cur.month) == (prev.year, prev.month):
            return  # 不是本月第一个交易日

        price = float(bars["close"].iloc[-1])
        if float(self.p["amount"]) > 0 and price > 0:
            shares = int(float(self.p["amount"]) / price)
        else:
            shares = int(self.p["qty"])
        if shares >= int(self.p["min_shares"]):
            ctx.buy(shares)
            ctx.log(f"定投买入 {shares} 股 @≈{price:.2f}")
