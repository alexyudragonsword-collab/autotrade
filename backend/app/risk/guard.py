"""持仓守护：止损 / 止盈 / 移动止损，自动平仓。

调度器周期调用 run_position_guard()：
- 逐持仓取最新价（券商行情 → BarStore 收盘价兜底），维护高水位 high_water_price
- 触发阈值（百分比，5 = 5%）后市价全平：守护单只减少敞口，不经限额类风控规则，
  但服从 kill switch（trading_enabled=False 时不动作，只在日志提示）
- 同一持仓已有在途卖单时不重复触发
"""

import logging

from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Order, Position, RiskConfig, Signal
from app.domain.enums import Market, NotifyLevel, OrderSide, OrderType, SignalStatus
from app.domain.schemas import NotifyEvent, OrderIntent

logger = logging.getLogger(__name__)

_OPEN_STATUSES = ("pending_submit", "submitted", "partially_filled")


async def _latest_price(broker: str, symbol: str) -> float | None:
    import asyncio

    from app.brokers.manager import get_broker_manager

    adapter = get_broker_manager().get_if_connected(broker)
    if adapter is not None:
        try:
            quote = await adapter.get_quote(symbol)
            if quote is not None and quote.price:
                return float(quote.price)
        except Exception:
            logger.debug("守护取 %s 行情失败", symbol)
    try:
        from app.data.store import get_bar_store

        price = await asyncio.to_thread(get_bar_store().last_close, symbol)
        return float(price) if price else None
    except Exception:
        return None


def _check_triggers(cfg: RiskConfig, pos: Position, price: float) -> str | None:
    """返回触发原因描述；未触发返回 None。阈值单位：百分比。"""
    if pos.avg_cost <= 0:
        return None
    if cfg.stop_loss_pct > 0 and price <= pos.avg_cost * (1 - cfg.stop_loss_pct / 100):
        loss = (price / pos.avg_cost - 1) * 100
        return f"止损触发：现价 {price:.3f} 较成本 {pos.avg_cost:.3f} 亏损 {abs(loss):.1f}%"
    if cfg.take_profit_pct > 0 and price >= pos.avg_cost * (1 + cfg.take_profit_pct / 100):
        gain = (price / pos.avg_cost - 1) * 100
        return f"止盈触发：现价 {price:.3f} 较成本 {pos.avg_cost:.3f} 盈利 {gain:.1f}%"
    hw = pos.high_water_price or pos.avg_cost
    if cfg.trailing_stop_pct > 0 and hw > 0 and price <= hw * (1 - cfg.trailing_stop_pct / 100):
        dd = (1 - price / hw) * 100
        return f"移动止损触发：现价 {price:.3f} 距高水位 {hw:.3f} 回撤 {dd:.1f}%"
    return None


async def run_position_guard() -> list[dict]:
    """执行一轮守护检查，返回触发记录（测试/日志用）。"""
    from app.execution.order_manager import get_order_manager
    from app.notify.dispatcher import get_dispatcher

    db = SessionLocal()
    triggered: list[dict] = []
    try:
        cfg = db.get(RiskConfig, 1)
        if cfg is None:
            return []
        guard_active = any((cfg.stop_loss_pct > 0, cfg.take_profit_pct > 0,
                            cfg.trailing_stop_pct > 0))

        positions = db.scalars(select(Position).where(Position.qty > 0)).all()
        for pos in positions:
            price = await _latest_price(pos.broker, pos.symbol)
            if price is None:
                continue
            # 高水位始终维护（即使守护未开启，方便随时启用移动止损）
            new_hw = max(pos.high_water_price or pos.avg_cost, price)
            if new_hw != pos.high_water_price:
                pos.high_water_price = new_hw
                db.commit()
            if not guard_active:
                continue

            reason = _check_triggers(cfg, pos, price)
            if reason is None:
                continue
            if not cfg.trading_enabled:
                logger.warning("守护触发但 kill switch 已关闭交易，跳过 %s：%s", pos.symbol, reason)
                continue
            # 已有在途卖单则不重复下单
            open_sell = db.scalar(select(Order).where(
                Order.broker == pos.broker, Order.symbol == pos.symbol,
                Order.side == "sell", Order.status.in_(_OPEN_STATUSES)))
            if open_sell is not None:
                continue

            sig = Signal(source="risk_guard",
                         dedup_key=f"guard:{pos.broker}:{pos.symbol}:{pos.id}:{price:.4f}",
                         symbol=pos.symbol, market=pos.market, action="sell",
                         quantity=pos.qty, order_type="market", price=price,
                         status=SignalStatus.RECEIVED, reject_reason=reason[:300])
            db.add(sig)
            try:
                db.commit()
            except Exception:
                db.rollback()
                continue
            db.refresh(sig)

            intent = OrderIntent(symbol=pos.symbol, market=Market(pos.market),
                                 side=OrderSide.SELL, order_type=OrderType.MARKET,
                                 qty=pos.qty, est_price=price, broker=pos.broker,
                                 strategy="risk_guard")
            order = await get_order_manager().submit(intent, signal_id=sig.id)
            sig.status = "failed" if order.status == "failed" else SignalStatus.ROUTED
            db.commit()
            triggered.append({"symbol": pos.symbol, "broker": pos.broker,
                              "reason": reason, "order_id": order.id})
            await get_dispatcher().emit(NotifyEvent(
                level=NotifyLevel.WARN, title="持仓守护平仓",
                body=reason,
                fields={"标的": pos.symbol, "账户": pos.broker,
                        "数量": pos.qty, "订单": f"#{order.id}"},
                broker=pos.broker))
            logger.warning("持仓守护平仓 %s（%s）：%s", pos.symbol, pos.broker, reason)
        return triggered
    finally:
        db.close()
