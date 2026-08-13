"""Paper 模拟券商：内存撮合，持仓/现金落库，重启不丢。

- market 单：按实时行情（其它已连接 broker 的 quote → BarStore 最近收盘价 → 信号价）立即全成
- limit 单：挂起，由 check_pending_limits()（调度器每分钟调用）按最新价触发
- 可配置 partial_fill_ratio / reject_probability 用于测试 OrderManager 状态机
"""

import asyncio
import itertools
import logging

from sqlalchemy import select

from app.brokers.base import BrokerAdapter, BrokerError
from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models import AppSetting, Position
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

_CASH_KEY = "paper_cash"


class PaperBroker(BrokerAdapter):
    markets = {Market.CN, Market.HK, Market.US, Market.CRYPTO}

    def __init__(self, name: str = "paper", quote_fn=None,
                 partial_fill_ratio: float = 1.0, fee_bps: float = 3.0,
                 initial_cash: float | None = None):
        super().__init__()
        self.name = name  # 账户名即 broker 标识，可多实例
        self._connected = False
        self._id_seq = itertools.count(1)
        self._quote_fn = quote_fn  # 可选外部行情源: async (symbol) -> float | None
        self.partial_fill_ratio = partial_fill_ratio
        self.fee_bps = fee_bps
        self.initial_cash = initial_cash
        # broker_order_id -> (OrderRequest, hint_price)
        self._pending_limits: dict[str, tuple[OrderRequest, float | None]] = {}

    @property
    def _cash_key(self) -> str:
        # 默认账户沿用旧键名，向后兼容既有数据
        return _CASH_KEY if self.name == "paper" else f"{_CASH_KEY}:{self.name}"

    # ---------- 连接 ----------

    async def connect(self) -> None:
        db = SessionLocal()
        try:
            if db.get(AppSetting, self._cash_key) is None:
                cash = self.initial_cash if self.initial_cash is not None \
                    else get_settings().paper_initial_cash
                db.add(AppSetting(key=self._cash_key, value=cash))
                db.commit()
        finally:
            db.close()
        self._connected = True
        logger.info("PaperBroker[%s] 已就绪", self.name)

    async def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    # ---------- 交易 ----------

    async def place_order(self, req: OrderRequest) -> BrokerOrderRef:
        if not self._connected:
            raise BrokerError("paper broker 未连接")
        oid = f"P{next(self._id_seq):08d}"
        if req.order_type == OrderType.MARKET:
            price = await self._resolve_price(req.symbol, hint=req.hint_price)
            if price is None:
                # 异步报告拒单（真实券商也是先受理再回报）
                asyncio.get_running_loop().call_soon(
                    asyncio.create_task,
                    self._emit_order_update(OrderUpdate(oid, OrderStatus.REJECTED,
                                                        error_msg=f"无法获取 {req.symbol} 行情价")),
                )
                return BrokerOrderRef(oid)
            asyncio.get_running_loop().call_soon(asyncio.create_task, self._fill(oid, req, price))
        else:
            if req.limit_price is None:
                raise BrokerError("限价单缺少 limit_price")
            self._pending_limits[oid] = (req, req.limit_price)
            asyncio.get_running_loop().call_soon(
                asyncio.create_task,
                self._emit_order_update(OrderUpdate(oid, OrderStatus.SUBMITTED)),
            )
        return BrokerOrderRef(oid)

    async def cancel_order(self, broker_order_id: str) -> None:
        if self._pending_limits.pop(broker_order_id, None) is not None:
            await self._emit_order_update(OrderUpdate(broker_order_id, OrderStatus.CANCELLED))
        else:
            raise BrokerError(f"订单 {broker_order_id} 不存在或已终态")

    async def check_pending_limits(self) -> None:
        """由调度器周期调用：检查挂起限价单是否触发。"""
        for oid, (req, limit) in list(self._pending_limits.items()):
            price = await self._resolve_price(req.symbol, hint=None)
            if price is None:
                continue
            hit = (req.side == OrderSide.BUY and price <= limit) or \
                  (req.side == OrderSide.SELL and price >= limit)
            if hit:
                del self._pending_limits[oid]
                await self._fill(oid, req, limit)

    async def get_positions(self) -> list[PositionSnapshot]:
        db = SessionLocal()
        try:
            rows = db.scalars(select(Position).where(Position.broker == self.name)).all()
            return [PositionSnapshot(r.symbol, Market(r.market), r.qty, r.avg_cost)
                    for r in rows if r.qty != 0]
        finally:
            db.close()

    async def get_account(self) -> AccountSnapshot:
        db = SessionLocal()
        try:
            cash_row = db.get(AppSetting, self._cash_key)
            cash = float(cash_row.value) if cash_row else 0.0
            net = cash
            for pos in await self.get_positions():
                price = await self._resolve_price(pos.symbol, hint=pos.avg_cost)
                net += pos.qty * (price or pos.avg_cost)
            return AccountSnapshot(cash=cash, net_value=net, buying_power=cash)
        finally:
            db.close()

    async def get_quote(self, symbol: str) -> Quote | None:
        price = await self._resolve_price(symbol, hint=None)
        return Quote(symbol, price) if price is not None else None

    # ---------- 内部 ----------

    async def _resolve_price(self, symbol: str, hint: float | None) -> float | None:
        if self._quote_fn is not None:
            try:
                price = await self._quote_fn(symbol)
                if price:
                    return float(price)
            except Exception:
                logger.warning("外部行情源获取 %s 失败", symbol)
        try:
            from app.data.store import get_bar_store

            price = await asyncio.to_thread(get_bar_store().last_close, symbol)
            if price:
                return float(price)
        except Exception:
            logger.debug("BarStore 无 %s 缓存", symbol)
        return hint

    async def _fill(self, oid: str, req: OrderRequest, price: float) -> None:
        fill_qty = req.qty * self.partial_fill_ratio
        fee = price * fill_qty * self.fee_bps / 10_000
        self._apply_position(req, fill_qty, price, fee)
        await self._emit_fill(FillEvent(oid, fill_qty, price, fee, broker_trade_id=f"T{oid}"))
        if fill_qty >= req.qty:
            status = OrderStatus.FILLED
        else:
            status = OrderStatus.PARTIALLY_FILLED
        await self._emit_order_update(
            OrderUpdate(oid, status, filled_qty=fill_qty, avg_fill_price=price)
        )

    def _apply_position(self, req: OrderRequest, qty: float, price: float, fee: float) -> None:
        db = SessionLocal()
        try:
            pos = db.scalar(select(Position).where(Position.broker == self.name,
                                                   Position.symbol == req.symbol))
            if pos is None:
                pos = Position(broker=self.name, symbol=req.symbol, market=req.market, qty=0.0, avg_cost=0.0)
                db.add(pos)
            cash_row = db.get(AppSetting, self._cash_key)
            cash = float(cash_row.value) if cash_row else 0.0
            if req.side == OrderSide.BUY:
                new_qty = pos.qty + qty
                pos.avg_cost = (pos.qty * pos.avg_cost + qty * price) / new_qty if new_qty else 0.0
                pos.qty = new_qty
                cash -= price * qty + fee
            else:
                pos.qty = max(0.0, pos.qty - qty)
                if pos.qty == 0:
                    pos.avg_cost = 0.0
                cash += price * qty - fee
            if cash_row:
                cash_row.value = cash
            db.commit()
        finally:
            db.close()
