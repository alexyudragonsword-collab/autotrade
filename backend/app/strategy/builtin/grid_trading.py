from app.screener.indicators import SMA
from app.strategy.base import Strategy, StrategyContext


class GridTrading(Strategy):
    """网格交易：以 N 日均线为中枢，价格每跌一格加一份仓、每涨一格减一份仓。

    目标仓位 = clamp(低于中枢的格数, 0, grids) × qty_per_grid，
    与当前持仓差额 ≥ 一份时才调整（死区防抖动）。适合震荡市，趋势市会亏损——
    建议搭配持仓守护的止损。
    """

    params = {"grid_pct": 3.0, "grids": 5, "qty_per_grid": 100, "center_period": 60}

    def on_bar(self, ctx: StrategyContext) -> None:
        n = int(self.p["center_period"])
        bars = ctx.history(n + 5)
        if len(bars) < n:
            return
        center = SMA(bars["close"], n).iloc[-1]
        price = bars["close"].iloc[-1]
        step = center * float(self.p["grid_pct"]) / 100
        if step <= 0:
            return

        # 价格低于中枢多少格（向下取整；高于中枢为 0）
        level = int(max(0.0, (center - price)) / step)
        level = min(level, int(self.p["grids"]))
        target = level * float(self.p["qty_per_grid"])
        diff = target - ctx.position()

        if diff >= float(self.p["qty_per_grid"]):
            ctx.buy(diff)
            ctx.log(f"格 {level}：加仓 {diff:g}（中枢 {center:.2f}，价 {price:.2f}）")
        elif diff <= -float(self.p["qty_per_grid"]):
            ctx.sell(-diff)
            ctx.log(f"格 {level}：减仓 {-diff:g}（中枢 {center:.2f}，价 {price:.2f}）")
