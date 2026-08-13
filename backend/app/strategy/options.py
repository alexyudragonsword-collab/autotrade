"""期权策略框架 + 内置备兑/车轮策略。

期权策略是**实盘专属**（无期权历史数据源，不进回测引擎），因此与股票策略不同：
- `on_run(ctx)` 为 async —— 运行时实时查期权链、按规则选合约
- 由调度 cron / 手动"运行"驱动（与本地股票策略相同的驱动方式）
- 产生的开平仓动作转成信号进入统一管道（风控两档卖方规则照常把关，
  mode=signal_only 时只提醒不下单）

上下文的期权链来源：优先执行账户自身；执行账户不支持期权链（如 paper）时
自动借用任一在线真实券商（IBKR/富途）的链数据——"paper 执行 + 真实链报价"
是模拟验证期权策略的推荐姿势。
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.domain.contracts import OptionContract, days_to_expiry
from app.domain.schemas import OptionChainItem

logger = logging.getLogger(__name__)


@dataclass
class OptionPosition:
    contract: OptionContract
    symbol: str
    qty: float  # 负 = 空头
    avg_cost: float
    multiplier: float

    @property
    def dte(self) -> int:
        return days_to_expiry(self.symbol) or 0


@dataclass
class OptionAction:
    symbol: str
    action: str  # "buy" | "sell"
    qty: float
    price: float | None  # 参考价（链报价 mid/last），风控估值与 paper 撮合用
    multiplier: float


class OptionStrategyContext(ABC):
    """单一标的视角：正股持仓 + 该标的的期权持仓 + 期权链查询 + 开平动作。"""

    underlying: str = ""

    @abstractmethod
    async def spot(self) -> float | None:
        """正股最新价。"""

    @abstractmethod
    def stock_qty(self) -> float: ...

    @abstractmethod
    def cash(self) -> float | None:
        """执行账户现金（现金担保判断参考）。"""

    @abstractmethod
    def option_positions(self) -> list[OptionPosition]:
        """该标的下的全部期权持仓。"""

    @abstractmethod
    async def expirations(self) -> list[str]: ...

    @abstractmethod
    async def select_contract(self, right: str, min_dte: int, max_dte: int,
                              otm_pct: float) -> OptionChainItem | None:
        """按规则选合约：
        - 到期日：dte ∈ [min_dte, max_dte] 中最近的一个
        - 行权价：Call 取 ≥ spot×(1+otm_pct/100) 的最低档；Put 取 ≤ spot×(1-otm_pct/100) 的最高档
        选不出（无链/无报价/无满足档位）返回 None。
        """

    @abstractmethod
    def sell_open(self, symbol: str, qty: float, price: float | None = None,
                  multiplier: float = 100.0) -> None: ...

    @abstractmethod
    def buy_close(self, symbol: str, qty: float, price: float | None = None,
                  multiplier: float = 100.0) -> None: ...

    def log(self, msg: str) -> None:  # noqa: B027
        pass


class OptionStrategy(ABC):
    """期权策略基类：实现 async on_run(ctx)，每个标的调用一次。"""

    params: dict = {}

    def __init__(self, **overrides):
        self.p = {**self.params, **overrides}

    @abstractmethod
    async def on_run(self, ctx: OptionStrategyContext) -> None: ...

    # ---- 公共助手 ----

    @staticmethod
    def short_positions(ctx: OptionStrategyContext, right: str) -> list[OptionPosition]:
        return [p for p in ctx.option_positions()
                if p.qty < 0 and p.contract.right == right]

    @staticmethod
    def mid_or_last(item: OptionChainItem) -> float | None:
        if item.bid is not None and item.ask is not None:
            return round((item.bid + item.ask) / 2, 3)
        return item.last


class CoveredCall(OptionStrategy):
    """备兑开仓：持有正股时滚动卖出虚值 Call 收权利金。

    每次运行：无空头 Call 且正股足额 → 卖出虚值 Call（张数=正股可备兑上限）；
    已有空头 Call 到期临近（dte ≤ roll_dte）→ 买回平仓，下次运行自动开新一期。
    """

    params = {"otm_pct": 5.0, "min_dte": 20, "max_dte": 45, "roll_dte": 3,
              "max_contracts": 0}  # 0 = 按正股数量自动

    async def on_run(self, ctx: OptionStrategyContext) -> None:
        shorts = self.short_positions(ctx, "C")

        # 1) 到期临近的空头 Call → 买回（滚动前半程；开新仓等下次运行，
        #    届时持仓/备兑额度已更新，避免同轮开平的竞态）
        rolled = False
        for pos in shorts:
            if pos.dte <= int(self.p["roll_dte"]):
                ctx.buy_close(pos.symbol, abs(pos.qty), multiplier=pos.multiplier)
                ctx.log(f"滚动：买回 {pos.symbol}（剩 {pos.dte} 天）")
                rolled = True
        if rolled:
            return
        if shorts:
            return  # 已有有效空头 Call，本期不再开仓

        # 2) 开新一期备兑 Call
        item = await ctx.select_contract("C", int(self.p["min_dte"]),
                                         int(self.p["max_dte"]), float(self.p["otm_pct"]))
        if item is None:
            ctx.log("未找到符合条件的 Call 合约，跳过")
            return
        coverable = int(ctx.stock_qty() // item.multiplier)
        if coverable <= 0:
            ctx.log(f"正股不足 {item.multiplier:g} 股，无法备兑，跳过")
            return
        qty = coverable if int(self.p["max_contracts"]) <= 0 \
            else min(coverable, int(self.p["max_contracts"]))
        price = self.mid_or_last(item)
        if price is None:
            ctx.log(f"{item.symbol} 无报价，跳过")
            return
        ctx.sell_open(item.symbol, qty, price=price, multiplier=item.multiplier)
        ctx.log(f"备兑卖出 {qty} 张 {item.symbol} @≈{price}")


class WheelStrategy(OptionStrategy):
    """车轮策略：现金担保卖 Put 收权利金；被行权接货后自动切换为备兑卖 Call，循环。

    每次运行：持有正股足额 → 走备兑 Call 腿；否则无空头 Put 时卖出虚值现金担保 Put。
    被行权（Put 变正股 / Call 交割正股）由持仓同步自然体现，下次运行自动换腿。
    """

    params = {"otm_pct": 5.0, "min_dte": 20, "max_dte": 45, "roll_dte": 3,
              "put_contracts": 1}

    async def on_run(self, ctx: OptionStrategyContext) -> None:
        # 滚动临近到期的空头（Put 与 Call 都处理）；有买回则本轮结束，
        # 开新仓等下次运行（持仓/担保额度更新后），避免同轮开平竞态
        rolled = False
        for right in ("P", "C"):
            for pos in self.short_positions(ctx, right):
                if pos.dte <= int(self.p["roll_dte"]):
                    ctx.buy_close(pos.symbol, abs(pos.qty), multiplier=pos.multiplier)
                    ctx.log(f"滚动：买回 {pos.symbol}（剩 {pos.dte} 天）")
                    rolled = True
        if rolled:
            return

        active_puts = self.short_positions(ctx, "P")
        active_calls = self.short_positions(ctx, "C")

        # 腿1：持有正股 → 备兑 Call
        probe = await ctx.select_contract("C", int(self.p["min_dte"]),
                                          int(self.p["max_dte"]), float(self.p["otm_pct"]))
        mult = probe.multiplier if probe else 100.0
        if ctx.stock_qty() >= mult:
            if active_calls:
                return
            if probe is None:
                ctx.log("未找到符合条件的 Call 合约，跳过")
                return
            qty = int(ctx.stock_qty() // probe.multiplier)
            price = self.mid_or_last(probe)
            if price is None:
                ctx.log(f"{probe.symbol} 无报价，跳过")
                return
            ctx.sell_open(probe.symbol, qty, price=price, multiplier=probe.multiplier)
            ctx.log(f"车轮-备兑腿：卖出 {qty} 张 {probe.symbol} @≈{price}")
            return

        # 腿2：无正股 → 现金担保 Put
        if active_puts:
            return
        item = await ctx.select_contract("P", int(self.p["min_dte"]),
                                         int(self.p["max_dte"]), float(self.p["otm_pct"]))
        if item is None:
            ctx.log("未找到符合条件的 Put 合约，跳过")
            return
        qty = max(1, int(self.p["put_contracts"]))
        need_cash = item.strike * item.multiplier * qty
        cash = ctx.cash()
        if cash is not None and cash < need_cash:
            ctx.log(f"现金 {cash:,.0f} 不足以担保 {qty} 张（需 {need_cash:,.0f}），跳过")
            return
        price = self.mid_or_last(item)
        if price is None:
            ctx.log(f"{item.symbol} 无报价，跳过")
            return
        ctx.sell_open(item.symbol, qty, price=price, multiplier=item.multiplier)
        ctx.log(f"车轮-Put腿：卖出 {qty} 张 {item.symbol} @≈{price}")
