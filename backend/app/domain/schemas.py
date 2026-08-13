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
    qty: float
    avg_cost: float


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

    @property
    def est_value(self) -> float:
        return abs(self.qty * (self.est_price or 0.0))


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


# ---------- API ----------


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
