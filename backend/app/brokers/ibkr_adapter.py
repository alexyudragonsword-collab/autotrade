"""盈透 IBKR 适配器。

依赖运行中的 TWS 或 IB Gateway（paper 端口 7497 / 实盘 7496）。
使用 ib_async（ib_insync 的社区维护 fork），原生 asyncio。
内置简单令牌桶节流应对 IB pacing 限制；Gateway 每日重启由
BrokerManager.health_check 自动重连兜底。
"""

import asyncio
import logging
import time as _time

from app.brokers.base import BrokerAdapter, BrokerError
from app.config import get_settings
from app.domain.enums import Market, OrderSide, OrderStatus, OrderType
from app.domain.schemas import (
    AccountSnapshot,
    BrokerOrderRef,
    FillEvent,
    OrderRequest,
    OrderUpdate,
    PositionSnapshot,
    Quote,
)

logger = logging.getLogger(__name__)

_IB_STATUS_MAP = {
    "PendingSubmit": OrderStatus.SUBMITTED,
    "PreSubmitted": OrderStatus.SUBMITTED,
    "Submitted": OrderStatus.SUBMITTED,
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELLED,
    "ApiCancelled": OrderStatus.CANCELLED,
    "Inactive": OrderStatus.REJECTED,
}


class _Throttle:
    """令牌桶：限制每秒消息数，规避 IB pacing violation。"""

    def __init__(self, rate: float = 40.0):
        self.rate = rate
        self._allowance = rate
        self._last = _time.monotonic()

    async def acquire(self) -> None:
        while True:
            now = _time.monotonic()
            self._allowance = min(self.rate, self._allowance + (now - self._last) * self.rate)
            self._last = now
            if self._allowance >= 1:
                self._allowance -= 1
                return
            await asyncio.sleep((1 - self._allowance) / self.rate)


class IbkrAdapter(BrokerAdapter):
    name = "ibkr"
    markets = {Market.US}

    def __init__(self):
        super().__init__()
        self._ib = None
        self._throttle = _Throttle()

    # ---------- 连接 ----------

    async def connect(self) -> None:
        try:
            from ib_async import IB
        except ImportError:
            raise BrokerError("未安装 ib_async（pip install 'autotrade[ibkr]'）")
        s = get_settings()
        ib = IB()
        try:
            await ib.connectAsync(s.ibkr_host, s.ibkr_port, clientId=s.ibkr_client_id, timeout=10)
        except Exception as e:
            raise BrokerError(f"连接 TWS/Gateway 失败（{s.ibkr_host}:{s.ibkr_port}）: {e}")
        self._ib = ib
        ib.orderStatusEvent += self._on_ib_order_status
        ib.execDetailsEvent += self._on_ib_exec
        ib.disconnectedEvent += lambda: logger.warning("IBKR 连接断开，等待健康检查自动重连")
        logger.info("IBKR 已连接（%s:%s clientId=%s）", s.ibkr_host, s.ibkr_port, s.ibkr_client_id)

    async def disconnect(self) -> None:
        if self._ib is not None:
            self._ib.disconnect()
            self._ib = None

    def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    # ---------- 事件桥接 ----------

    def _on_ib_order_status(self, trade) -> None:
        status = _IB_STATUS_MAP.get(trade.orderStatus.status)
        if status is None:
            return
        filled = float(trade.orderStatus.filled or 0)
        if status == OrderStatus.SUBMITTED and filled > 0:
            status = OrderStatus.PARTIALLY_FILLED
        update = OrderUpdate(
            broker_order_id=str(trade.order.orderId),
            status=status,
            filled_qty=filled,
            avg_fill_price=float(trade.orderStatus.avgFillPrice or 0) or None,
        )
        asyncio.ensure_future(self._emit_order_update(update))

    def _on_ib_exec(self, trade, fill) -> None:
        event = FillEvent(
            broker_order_id=str(trade.order.orderId),
            qty=float(fill.execution.shares),
            price=float(fill.execution.price),
            fee=0.0,  # 佣金随后到 commissionReport，v1 简化不回填
            broker_trade_id=str(fill.execution.execId),
        )
        asyncio.ensure_future(self._emit_fill(event))

    # ---------- 交易 ----------

    @staticmethod
    def _contract(symbol: str):
        from ib_async import Stock

        ticker = symbol.split(".", 1)[-1]
        return Stock(ticker, "SMART", "USD")

    async def place_order(self, req: OrderRequest) -> BrokerOrderRef:
        from ib_async import LimitOrder, MarketOrder

        if self._ib is None:
            raise BrokerError("ibkr 未连接")
        await self._throttle.acquire()
        action = "BUY" if req.side == OrderSide.BUY else "SELL"
        if req.order_type == OrderType.LIMIT:
            order = LimitOrder(action, req.qty, req.limit_price)
        else:
            order = MarketOrder(action, req.qty)
        trade = self._ib.placeOrder(self._contract(req.symbol), order)
        return BrokerOrderRef(str(trade.order.orderId))

    async def cancel_order(self, broker_order_id: str) -> None:
        if self._ib is None:
            raise BrokerError("ibkr 未连接")
        await self._throttle.acquire()
        for trade in self._ib.openTrades():
            if str(trade.order.orderId) == broker_order_id:
                self._ib.cancelOrder(trade.order)
                return
        raise BrokerError(f"未找到可撤订单 {broker_order_id}")

    async def get_order(self, broker_order_id: str) -> OrderUpdate | None:
        if self._ib is None:
            return None
        for trade in self._ib.trades():
            if str(trade.order.orderId) == broker_order_id:
                status = _IB_STATUS_MAP.get(trade.orderStatus.status, OrderStatus.SUBMITTED)
                filled = float(trade.orderStatus.filled or 0)
                if status == OrderStatus.SUBMITTED and filled > 0:
                    status = OrderStatus.PARTIALLY_FILLED
                return OrderUpdate(broker_order_id, status, filled,
                                   float(trade.orderStatus.avgFillPrice or 0) or None)
        return None

    async def get_positions(self) -> list[PositionSnapshot]:
        if self._ib is None:
            raise BrokerError("ibkr 未连接")
        await self._throttle.acquire()
        positions = await self._ib.reqPositionsAsync()
        out = []
        for pos in positions:
            if pos.contract.secType != "STK":
                continue
            out.append(PositionSnapshot(
                f"US.{pos.contract.symbol}", Market.US,
                float(pos.position), float(pos.avgCost or 0)))
        return out

    async def get_account(self) -> AccountSnapshot:
        if self._ib is None:
            raise BrokerError("ibkr 未连接")
        await self._throttle.acquire()
        values = self._ib.accountValues()
        acc = {v.tag: v.value for v in values if v.currency in ("USD", "")}
        return AccountSnapshot(
            cash=float(acc.get("TotalCashValue", 0) or 0),
            net_value=float(acc.get("NetLiquidation", 0) or 0),
            buying_power=float(acc.get("BuyingPower", 0) or 0),
        )

    async def get_quote(self, symbol: str) -> Quote | None:
        if self._ib is None:
            return None
        try:
            await self._throttle.acquire()
            ticker = self._ib.reqMktData(self._contract(symbol), snapshot=True)
            for _ in range(20):  # 最多等 2 秒
                await asyncio.sleep(0.1)
                price = ticker.marketPrice()
                if price and price == price:  # 非 NaN
                    return Quote(symbol, float(price))
            return None
        except Exception:
            logger.warning("IBKR 行情获取失败: %s", symbol)
            return None
