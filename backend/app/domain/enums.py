from enum import StrEnum


class Market(StrEnum):
    CN = "CN"  # A股（沪深）
    HK = "HK"  # 港股
    US = "US"  # 美股
    CRYPTO = "CRYPTO"  # 预留


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class SignalAction(StrEnum):
    BUY = "buy"
    SELL = "sell"
    CLOSE = "close"  # 平掉全部持仓


class SignalSource(StrEnum):
    TRADINGVIEW = "tradingview"
    SCREENER = "screener"
    STRATEGY = "strategy"
    MANUAL = "manual"


class SignalStatus(StrEnum):
    RECEIVED = "received"
    DUPLICATE = "duplicate"
    REJECTED_RISK = "rejected_risk"
    REJECTED = "rejected"  # 策略未启用 / broker 离线等
    ROUTED = "routed"  # 已提交给券商
    EXECUTED = "executed"  # 订单终态成交
    FAILED = "failed"


class OrderStatus(StrEnum):
    PENDING_SUBMIT = "pending_submit"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.FAILED,
        )


class StrategyMode(StrEnum):
    SIGNAL_ONLY = "signal_only"  # 只记录并通知，不下单
    LIVE = "live"  # 走风控后真实下单


class NotifyLevel(StrEnum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"

    @property
    def rank(self) -> int:
        return {"info": 0, "warn": 1, "error": 2}[self.value]


class BacktestStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
