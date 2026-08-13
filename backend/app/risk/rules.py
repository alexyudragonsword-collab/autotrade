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
    position_qty: float  # 该标的当前持仓
    position_value: float  # 该标的当前持仓市值
    total_exposure: float  # 全部持仓市值
    orders_today: int  # 当日已提交订单数
    realized_pnl_today: float  # 当日已实现盈亏（负=亏损）


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


class WhitelistRule(RiskRule):
    name = "symbol_whitelist"

    def check(self, intent, ctx):
        wl = ctx.config.symbol_whitelist or []
        if wl and intent.symbol not in wl:
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


class MaxPositionValueRule(RiskRule):
    name = "max_position_value_per_symbol"

    def check(self, intent, ctx):
        if intent.side != OrderSide.BUY:
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
        if intent.side != OrderSide.BUY:
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
        if intent.side == OrderSide.SELL and intent.qty > ctx.position_qty:
            return self.block(
                f"卖出数量 {intent.qty} 超过当前持仓 {ctx.position_qty}（不允许做空）")
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
        WhitelistRule(),
        SellExceedsPositionRule(),
        TradingHoursRule(now_fn),
        MaxOrderValueRule(),
        MaxPositionValueRule(),
        MaxExposureRule(),
        MaxOrdersPerDayRule(),
        DailyLossRule(),
    ]
