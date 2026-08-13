"""券商适配器抽象 —— 多市场解耦核心。"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from app.domain.enums import Market
from app.domain.schemas import (
    AccountSnapshot,
    BrokerOrderRef,
    FillEvent,
    OptionChainItem,
    OrderRequest,
    OrderUpdate,
    PositionSnapshot,
    Quote,
)

logger = logging.getLogger(__name__)

OrderUpdateCb = Callable[[OrderUpdate], Awaitable[None]]
FillCb = Callable[[FillEvent], Awaitable[None]]


class BrokerError(Exception):
    pass


class BrokerAdapter(ABC):
    name: str = ""
    markets: set[Market] = set()

    def __init__(self):
        self._order_update_cbs: list[OrderUpdateCb] = []
        self._fill_cbs: list[FillCb] = []

    # ---------- 连接 ----------
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    # ---------- 交易 ----------
    @abstractmethod
    async def place_order(self, req: OrderRequest) -> BrokerOrderRef: ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> None: ...

    @abstractmethod
    async def get_positions(self) -> list[PositionSnapshot]: ...

    @abstractmethod
    async def get_account(self) -> AccountSnapshot: ...

    # ---------- 行情（可选能力）----------
    async def get_quote(self, symbol: str) -> Quote | None:
        return None

    # ---------- 期权（可选能力）----------
    async def get_option_expirations(self, underlying: str) -> list[str]:
        """标的可交易期权的到期日列表（YYYYMMDD 升序）。"""
        raise BrokerError(f"{self.name} 不支持期权")

    async def get_option_chain(self, underlying: str, expiry: str,
                               with_quotes: bool = False,
                               strikes_around: int | None = None) -> list[OptionChainItem]:
        """某到期日的期权链；with_quotes 时附带报价（可能受限流约束）。"""
        raise BrokerError(f"{self.name} 不支持期权")

    async def get_contract_multiplier(self, symbol: str) -> float | None:
        """期权合约乘数；未知返回 None（调用方用市场默认值兜底）。"""
        return None

    # ---------- 事件回调 ----------
    def on_order_update(self, cb: OrderUpdateCb) -> None:
        self._order_update_cbs.append(cb)

    def on_fill(self, cb: FillCb) -> None:
        self._fill_cbs.append(cb)

    async def _emit_order_update(self, update: OrderUpdate) -> None:
        for cb in self._order_update_cbs:
            try:
                await cb(update)
            except Exception:
                logger.exception("[%s] order_update 回调异常", self.name)

    async def _emit_fill(self, fill: FillEvent) -> None:
        for cb in self._fill_cbs:
            try:
                await cb(fill)
            except Exception:
                logger.exception("[%s] fill 回调异常", self.name)
