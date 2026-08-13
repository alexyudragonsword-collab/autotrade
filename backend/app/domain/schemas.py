from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel

from app.domain.enums import Market, NotifyLevel, OrderSide, OrderType, SignalAction

# ---------- 信号 ----------


class NormalizedSignal(BaseModel):
    """TV 告警 / 内部策略统一后的标准信号。"""

    source: str = "tradingview"
    dedup_key: str
    strategy: str
    symbol: str  # 内部规范格式: US.AAPL / HK.00700 / SH.600519 / SZ.000001
    market: Market
    action: SignalAction
    quantity: float | None = None
    order_type: OrderType = OrderType.MARKET
    price: float | None = None
    raw: dict = {}


# ---------- 券商层 DTO ----------


@dataclass
class OrderRequest:
    symbol: str
    market: Market
    side: OrderSide
    order_type: OrderType
    qty: float
    limit_price: float | None = None
    hint_price: float | None = None  # 信号参考价；paper 无实时行情时的撮合兜底价，真实券商忽略
    multiplier: float = 1.0  # 合约乘数（期权），股票为 1


@dataclass
class BrokerOrderRef:
    broker_order_id: str


@dataclass
class OrderUpdate:
    broker_order_id: str
    status: str  # OrderStatus 值
    filled_qty: float = 0.0
    avg_fill_price: float | None = None
    error_msg: str | None = None


@dataclass
class FillEvent:
    broker_order_id: str
    qty: float
    price: float
    fee: float = 0.0
    broker_trade_id: str | None = None


@dataclass
class PositionSnapshot:
    symbol: str
    market: Market
    qty: float  # 负数 = 空头（期权卖方）
    avg_cost: float  # 每股/每单位口径（不含乘数）
    multiplier: float = 1.0


@dataclass
class AccountSnapshot:
    cash: float
    net_value: float
    buying_power: float


@dataclass
class Quote:
    symbol: str
    price: float
    ts: datetime | None = None


@dataclass
class OptionChainItem:
    """期权链条目（适配器返回，API 层按行权价分组）。"""

    symbol: str  # 内部规范期权符号
    strike: float
    right: str  # "C" | "P"
    multiplier: float
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    open_interest: float | None = None


# ---------- 风控 ----------


@dataclass
class OrderIntent:
    symbol: str
    market: Market
    side: OrderSide
    order_type: OrderType
    qty: float
    est_price: float | None  # 估算价（限价单用 limit，市价单用最新行情/信号价）
    broker: str
    strategy: str | None = None
    multiplier: float = 1.0

    @property
    def est_value(self) -> float:
        return abs(self.qty * (self.est_price or 0.0) * self.multiplier)


@dataclass
class RiskDecision:
    allowed: bool
    rule_name: str = ""
    reason: str = ""


# ---------- 通知 ----------


@dataclass
class NotifyEvent:
    level: NotifyLevel
    title: str
    body: str
    fields: dict = field(default_factory=dict)
    # 路由元数据：渠道可按策略/账户过滤；为 None 的事件（系统级）投递到所有渠道
    strategy: str | None = None
    broker: str | None = None


# ---------- API ----------


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
