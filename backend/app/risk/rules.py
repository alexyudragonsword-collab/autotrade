"""风控规则集。每条规则: check(intent, ctx) -> RiskDecision。"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from app.db.models import RiskConfig
from app.domain.enums import Market, OrderSide
from app.domain.schemas import OrderIntent, RiskDecision

logger = logging.getLogger(__name__)


@dataclass
class RiskContext:
    """规则求值所需的账户/当日统计快照（由 RiskEngine 组装）。"""

    config: RiskConfig
    position_qty: float  # 该标的当前持仓（可为负=空头）
    position_value: float  # 该标的当前持仓市值（|qty|×成本×乘数）
    total_exposure: float  # 全部持仓市值（Σ|qty|×成本×乘数）
    orders_today: int  # 当日已提交订单数
    realized_pnl_today: float  # 当日已实现盈亏（负=亏损）
    # ---- 期权卖方专用（股票单为 0/None）----
    multiplier: float = 1.0
    underlying_qty: float = 0.0  # 该期权标的正股在同账户的持仓
    short_call_qty_same_underlying: float = 0.0  # 同标的已有空头 Call 张数合计
    short_put_reserved: float = 0.0  # 已有空头 Put 占用的现金担保（Σ 行权价×乘数×张数）
    short_option_notional: float = 0.0  # 全部空头期权名义（Σ 行权价×乘数×张数）
    account_cash: float | None = None  # 账户现金；None = 无法获取


class RiskRule(ABC):
    name = ""

    @abstractmethod
    def check(self, intent: OrderIntent, ctx: RiskContext) -> RiskDecision: ...

    def allow(self) -> RiskDecision:
        return RiskDecision(True, self.name)

    def block(self, reason: str) -> RiskDecision:
        return RiskDecision(False, self.name, reason)


class KillSwitchRule(RiskRule):
    name = "kill_switch"

    def check(self, intent, ctx):
        if not ctx.config.trading_enabled:
            return self.block("全局交易开关已关闭（kill switch）")
        return self.allow()


class OptionsEnabledRule(RiskRule):
    name = "options_enabled"

    def check(self, intent, ctx):
        from app.domain.contracts import is_option

        if not is_option(intent.symbol):
            return self.allow()
        if not ctx.config.options_trading_enabled:
            return self.block("期权交易未开启（风控设置中启用）")
        if intent.market == Market.CN:
            return self.block("暂不支持 A股期权")
        return self.allow()


class WhitelistRule(RiskRule):
    name = "symbol_whitelist"

    def check(self, intent, ctx):
        from app.domain.contracts import underlying_of

        wl = ctx.config.symbol_whitelist or []
        # 期权按标的正股匹配白名单
        if wl and underlying_of(intent.symbol) not in wl:
            return self.block(f"{intent.symbol} 不在标的白名单内")
        return self.allow()


class MaxOrderValueRule(RiskRule):
    name = "max_order_value"

    def check(self, intent, ctx):
        if intent.est_price is None:
            return self.block("无法估算订单金额（缺少价格），已拒绝")
        if intent.est_value > ctx.config.max_order_value:
            return self.block(
                f"单笔金额 {intent.est_value:,.0f} 超过上限 {ctx.config.max_order_value:,.0f}")
        return self.allow()


def _is_buy_to_close(intent, ctx) -> bool:
    """买入回补空头（减风险操作），不受仓位限额约束。"""
    return intent.side == OrderSide.BUY and ctx.position_qty < 0 \
        and intent.qty <= -ctx.position_qty


class MaxPositionValueRule(RiskRule):
    name = "max_position_value_per_symbol"

    def check(self, intent, ctx):
        if intent.side != OrderSide.BUY or _is_buy_to_close(intent, ctx):
            return self.allow()
        projected = ctx.position_value + intent.est_value
        if projected > ctx.config.max_position_value_per_symbol:
            return self.block(
                f"{intent.symbol} 持仓市值将达 {projected:,.0f}，超过单标的上限 "
                f"{ctx.config.max_position_value_per_symbol:,.0f}")
        return self.allow()


class MaxExposureRule(RiskRule):
    name = "max_total_exposure"

    def check(self, intent, ctx):
        if intent.side != OrderSide.BUY or _is_buy_to_close(intent, ctx):
            return self.allow()
        projected = ctx.total_exposure + intent.est_value
        if projected > ctx.config.max_total_exposure:
            return self.block(
                f"总敞口将达 {projected:,.0f}，超过上限 {ctx.config.max_total_exposure:,.0f}")
        return self.allow()


class MaxOrdersPerDayRule(RiskRule):
    name = "max_orders_per_day"

    def check(self, intent, ctx):
        if ctx.orders_today >= ctx.config.max_orders_per_day:
            return self.block(f"当日订单数已达上限 {ctx.config.max_orders_per_day}")
        return self.allow()


class DailyLossRule(RiskRule):
    name = "max_daily_loss"

    def check(self, intent, ctx):
        loss = -ctx.realized_pnl_today
        if loss >= ctx.config.max_daily_loss:
            return self.block(
                f"当日已实现亏损 {loss:,.0f} 达到上限 {ctx.config.max_daily_loss:,.0f}，停止开新仓")
        return self.allow()


class SellExceedsPositionRule(RiskRule):
    name = "sell_exceeds_position"

    def check(self, intent, ctx):
        from app.domain.contracts import is_option

        if is_option(intent.symbol):
            return self.allow()  # 期权允许卖出开仓，由备兑/裸卖规则把关
        if intent.side == OrderSide.SELL and intent.qty > ctx.position_qty:
            return self.block(
                f"卖出数量 {intent.qty} 超过当前持仓 {ctx.position_qty}（股票不允许做空）")
        return self.allow()


def _opening_short_qty(intent, ctx) -> float:
    """本单会新增的空头张数（卖出量超出现有多头的部分）。"""
    return max(0.0, intent.qty - max(ctx.position_qty, 0.0))


class CoveredOrSecuredRule(RiskRule):
    """默认档卖方风控：卖 Call 须备兑（持有足额正股），卖 Put 须现金担保。"""

    name = "covered_or_secured"

    def check(self, intent, ctx):
        from app.domain.contracts import OptionContract

        contract = OptionContract.parse(intent.symbol)
        if contract is None or intent.side != OrderSide.SELL:
            return self.allow()
        if ctx.config.allow_naked_selling:
            return self.allow()  # 裸卖档由 NakedNotionalRule 把关
        opening = _opening_short_qty(intent, ctx)
        if opening <= 0:
            return self.allow()  # 纯平多仓，不产生新空头

        if contract.right == "C":
            need_shares = (ctx.short_call_qty_same_underlying + opening) * ctx.multiplier
            if ctx.underlying_qty < need_shares:
                return self.block(
                    f"非备兑卖出 CALL：需持有 ≥ {need_shares:g} 股 {contract.underlying}"
                    f"（当前 {ctx.underlying_qty:g} 股）。如需裸卖请在风控设置中显式开启")
            return self.allow()

        # PUT：现金担保
        need_cash = ctx.short_put_reserved + contract.strike * ctx.multiplier * opening
        if ctx.account_cash is None:
            return self.block("无法获取账户现金，现金担保 PUT 校验失败（fail-closed）")
        if ctx.account_cash < need_cash:
            return self.block(
                f"现金担保不足：卖出 PUT 需预留 {need_cash:,.0f}"
                f"（含已有空头占用），当前现金 {ctx.account_cash:,.0f}")
        return self.allow()


class NakedNotionalRule(RiskRule):
    """裸卖档：空头期权名义总额（Σ 行权价×乘数×张数）不得超上限。"""

    name = "naked_notional"

    def check(self, intent, ctx):
        from app.domain.contracts import OptionContract

        contract = OptionContract.parse(intent.symbol)
        if contract is None or intent.side != OrderSide.SELL:
            return self.allow()
        if not ctx.config.allow_naked_selling:
            return self.allow()
        opening = _opening_short_qty(intent, ctx)
        if opening <= 0:
            return self.allow()
        projected = ctx.short_option_notional + contract.strike * ctx.multiplier * opening
        if projected > ctx.config.max_short_option_notional:
            return self.block(
                f"空头期权名义将达 {projected:,.0f}，超过上限 "
                f"{ctx.config.max_short_option_notional:,.0f}")
        return self.allow()


# 各市场常规交易时段（本地交易所时区）
_MARKET_HOURS: dict[Market, tuple[str, list[tuple[time, time]]]] = {
    Market.CN: ("Asia/Shanghai", [(time(9, 30), time(11, 30)), (time(13, 0), time(15, 0))]),
    Market.HK: ("Asia/Hong_Kong", [(time(9, 30), time(12, 0)), (time(13, 0), time(16, 0))]),
    Market.US: ("America/New_York", [(time(9, 30), time(16, 0))]),
}


class TradingHoursRule(RiskRule):
    name = "trading_hours"

    def __init__(self, now_fn=None):
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    def check(self, intent, ctx):
        if not ctx.config.trading_hours_enabled:
            return self.allow()
        spec = _MARKET_HOURS.get(intent.market)
        if spec is None:
            return self.allow()
        tz, sessions = spec
        local = self._now().astimezone(ZoneInfo(tz))
        if local.weekday() >= 5:
            return self.block(f"{intent.market} 周末休市")
        t = local.time()
        if not any(start <= t <= end for start, end in sessions):
            return self.block(f"{intent.market} 当前不在交易时段（交易所时间 {local:%H:%M}）")
        return self.allow()


def default_rules(now_fn=None) -> list[RiskRule]:
    return [
        KillSwitchRule(),
        OptionsEnabledRule(),
        WhitelistRule(),
        SellExceedsPositionRule(),
        CoveredOrSecuredRule(),
        NakedNotionalRule(),
        TradingHoursRule(now_fn),
        MaxOrderValueRule(),
        MaxPositionValueRule(),
        MaxExposureRule(),
        MaxOrdersPerDayRule(),
        DailyLossRule(),
    ]
