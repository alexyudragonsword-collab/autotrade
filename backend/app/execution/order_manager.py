"""OrderManager：下单、成交回报、订单状态机、超时对账。"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.brokers.base import BrokerError
from app.brokers.manager import BrokerManager
from app.db.base import SessionLocal
from app.db.models import Order, TradeFill
from app.domain.enums import Market, OrderStatus
from app.domain.schemas import FillEvent, OrderIntent, OrderRequest, OrderUpdate
from app.notify.dispatcher import get_dispatcher, order_event

logger = logging.getLogger(__name__)

# 状态机允许的迁移（防止乱序回报把终态改回中间态）
_ALLOWED = {
    OrderStatus.PENDING_SUBMIT: {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED,
                                 OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.FAILED},
    OrderStatus.SUBMITTED: {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
                            OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.FAILED},
    OrderStatus.PARTIALLY_FILLED: {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
                                   OrderStatus.CANCELLED, OrderStatus.FAILED},
}


class OrderManager:
    def __init__(self, brokers: BrokerManager):
        self.brokers = brokers

    def attach_callbacks(self) -> None:
        """注册所有 broker 的回报回调（启动时调用一次）。"""
        for adapter in self.brokers._adapters.values():
            self.attach_adapter(adapter)

    def attach_adapter(self, adapter) -> None:
        """为运行时新增的账户挂接回报回调。"""
        adapter.on_order_update(self._on_order_update)
        adapter.on_fill(self._on_fill)

    # ---------- 下单 ----------

    async def submit(self, intent: OrderIntent, signal_id: int | None = None) -> Order:
        db = SessionLocal()
        try:
            order = Order(
                signal_id=signal_id, broker=intent.broker, symbol=intent.symbol,
                market=intent.market, side=intent.side, order_type=intent.order_type,
                qty=intent.qty, multiplier=intent.multiplier or 1.0,
                limit_price=intent.est_price if intent.order_type == "limit" else None,
                status=OrderStatus.PENDING_SUBMIT,
            )
            db.add(order)
            db.commit()
            db.refresh(order)

            try:
                adapter = self.brokers.get(intent.broker)
                ref = await adapter.place_order(OrderRequest(
                    symbol=intent.symbol, market=intent.market, side=intent.side,
                    order_type=intent.order_type, qty=intent.qty,
                    limit_price=order.limit_price, hint_price=intent.est_price,
                    multiplier=intent.multiplier or 1.0,
                ))
                order.broker_order_id = ref.broker_order_id
                if order.status == OrderStatus.PENDING_SUBMIT:
                    order.status = OrderStatus.SUBMITTED
                db.commit()
            except BrokerError as e:
                order.status = OrderStatus.FAILED
                order.error_msg = str(e)[:300]
                db.commit()
                await get_dispatcher().emit(order_event(order, level="error"))
            return order
        finally:
            db.close()

    async def cancel(self, order_id: int) -> None:
        db = SessionLocal()
        try:
            order = db.get(Order, order_id)
            if order is None:
                raise ValueError(f"订单 {order_id} 不存在")
            if OrderStatus(order.status).is_terminal:
                raise ValueError(f"订单已是终态 {order.status}，无法撤销")
            adapter = self.brokers.get(order.broker)
            await adapter.cancel_order(order.broker_order_id)
        finally:
            db.close()

    # ---------- 回报 ----------

    async def _on_order_update(self, update: OrderUpdate) -> None:
        db = SessionLocal()
        try:
            order = db.scalar(select(Order).where(Order.broker_order_id == update.broker_order_id)
                              .order_by(Order.id.desc()))
            if order is None:
                logger.warning("收到未知订单回报: %s", update.broker_order_id)
                return
            current = OrderStatus(order.status)
            new = OrderStatus(update.status)
            if current.is_terminal or (current in _ALLOWED and new not in _ALLOWED[current]):
                logger.debug("忽略订单 %s 非法状态迁移 %s→%s", order.id, current, new)
                return
            order.status = new
            if update.filled_qty:
                order.filled_qty = max(order.filled_qty, update.filled_qty)
            if update.avg_fill_price:
                order.avg_fill_price = update.avg_fill_price
            if update.error_msg:
                order.error_msg = update.error_msg[:300]
            db.commit()
            from app.events import get_event_bus

            get_event_bus().publish("order_update", {
                "id": order.id, "broker": order.broker, "symbol": order.symbol,
                "side": order.side, "qty": order.qty, "filled_qty": order.filled_qty,
                "avg_fill_price": order.avg_fill_price, "status": order.status,
            })
            if new.is_terminal:
                await self._finalize_signal(db, order)
                level = "info" if new == OrderStatus.FILLED else "warn"
                await get_dispatcher().emit(order_event(order, level=level))
        finally:
            db.close()

    async def _on_fill(self, fill: FillEvent) -> None:
        db = SessionLocal()
        try:
            order = db.scalar(select(Order).where(Order.broker_order_id == fill.broker_order_id)
                              .order_by(Order.id.desc()))
            if order is None:
                logger.warning("收到未知成交回报: %s", fill.broker_order_id)
                return
            # 减仓成交按「成交前」持仓均价落已实现盈亏（含合约乘数）：
            # 卖出减多仓 / 买入回补空仓（期权卖方）
            realized = None
            from app.db.models import Position

            pos = db.scalar(select(Position).where(Position.broker == order.broker,
                                                   Position.symbol == order.symbol))
            mult = order.multiplier or 1.0
            if pos is not None and pos.avg_cost:
                if order.side == "sell" and pos.qty > 0:
                    closed = min(fill.qty, pos.qty)
                    realized = (fill.price - pos.avg_cost) * closed * mult - fill.fee
                elif order.side == "buy" and pos.qty < 0:
                    closed = min(fill.qty, -pos.qty)
                    realized = (pos.avg_cost - fill.price) * closed * mult - fill.fee
            db.add(TradeFill(order_id=order.id, broker_trade_id=fill.broker_trade_id,
                             qty=fill.qty, price=fill.price, fee=fill.fee,
                             realized_pnl=realized))
            db.commit()
        finally:
            db.close()

    async def _finalize_signal(self, db, order: Order) -> None:
        if order.signal_id is None:
            return
        from app.db.models import Signal  # 避免顶层循环导入

        sig = db.get(Signal, order.signal_id)
        if sig is None:
            return
        sig.status = "executed" if order.status == OrderStatus.FILLED else "failed"
        db.commit()

    # ---------- 对账 ----------

    async def reconcile_stale(self, timeout_seconds: int = 120) -> None:
        """submitted 超时无回报的订单，主动向券商查单（调度器周期调用）。"""
        db = SessionLocal()
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
            stale = db.scalars(select(Order).where(
                Order.status.in_([OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED]),
                Order.updated_at < cutoff,
            )).all()
            for order in stale:
                adapter = self.brokers.get_if_connected(order.broker)
                if adapter is None or order.broker_order_id is None:
                    continue
                get_order = getattr(adapter, "get_order", None)
                if get_order is None:
                    continue
                try:
                    snapshot = await get_order(order.broker_order_id)
                    if snapshot is not None:
                        await self._on_order_update(snapshot)
                except Exception:
                    logger.exception("对账订单 %s 失败", order.id)
        finally:
            db.close()

    # ---------- 持仓同步 ----------

    async def sync_positions(self) -> None:
        from app.db.models import Position

        db = SessionLocal()
        try:
            for name in list(self.brokers._adapters):
                adapter = self.brokers.get_if_connected(name)
                if adapter is None or name == "paper":  # paper 持仓由自身维护
                    continue
                try:
                    snapshots = await adapter.get_positions()
                except Exception:
                    logger.exception("同步 %s 持仓失败", name)
                    continue
                db.query(Position).filter(Position.broker == name).delete()
                for s in snapshots:
                    db.add(Position(broker=name, symbol=s.symbol, market=Market(s.market),
                                    qty=s.qty, avg_cost=s.avg_cost,
                                    multiplier=getattr(s, "multiplier", 1.0) or 1.0))
                db.commit()
        finally:
            db.close()


_order_manager: OrderManager | None = None


def get_order_manager() -> OrderManager:
    global _order_manager
    if _order_manager is None:
        from app.brokers.manager import get_broker_manager

        _order_manager = OrderManager(get_broker_manager())
        _order_manager.attach_callbacks()
    return _order_manager
