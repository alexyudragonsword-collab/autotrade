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
    """返回触发原因描述；未触发返回 None。阈值单位：百分比。

    多头（qty>0）：价格下跌止损、上涨止盈、距高水位回撤移动止损。
    空头（qty<0，期权卖方）：方向全部反转——价格上涨止损、下跌止盈、
    距低水位反弹移动止损（high_water_price 列对空头存低水位）。
    """
    if pos.avg_cost <= 0:
        return None
    if pos.qty > 0:
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
    # ---- 空头 ----
    if cfg.stop_loss_pct > 0 and price >= pos.avg_cost * (1 + cfg.stop_loss_pct / 100):
        loss = (price / pos.avg_cost - 1) * 100
        return f"空头止损触发：现价 {price:.3f} 较开仓 {pos.avg_cost:.3f} 上涨 {loss:.1f}%"
    if cfg.take_profit_pct > 0 and price <= pos.avg_cost * (1 - cfg.take_profit_pct / 100):
        gain = (1 - price / pos.avg_cost) * 100
        return f"空头止盈触发：现价 {price:.3f} 较开仓 {pos.avg_cost:.3f} 下跌 {gain:.1f}%（权利金收益落袋）"
    lw = pos.high_water_price or pos.avg_cost
    if cfg.trailing_stop_pct > 0 and lw > 0 and price >= lw * (1 + cfg.trailing_stop_pct / 100):
        bounce = (price / lw - 1) * 100
        return f"空头移动止损触发：现价 {price:.3f} 距低水位 {lw:.3f} 反弹 {bounce:.1f}%"
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

        positions = db.scalars(select(Position).where(Position.qty != 0)).all()
        for pos in positions:
            price = await _latest_price(pos.broker, pos.symbol)
            if price is None:
                continue
            # 水位始终维护（多头存高水位取 max，空头存低水位取 min）
            if pos.qty > 0:
                new_wm = max(pos.high_water_price or pos.avg_cost, price)
            else:
                new_wm = min(pos.high_water_price or pos.avg_cost, price)
            if new_wm != pos.high_water_price:
                pos.high_water_price = new_wm
                db.commit()
            if not guard_active:
                continue

            reason = _check_triggers(cfg, pos, price)
            if reason is None:
                continue
            if not cfg.trading_enabled:
                logger.warning("守护触发但 kill switch 已关闭交易，跳过 %s：%s", pos.symbol, reason)
                continue
            triggered_row = await _submit_guard_close(db, pos, price, reason, "risk_guard")
            if triggered_row is not None:
                triggered.append(triggered_row)
        return triggered
    finally:
        db.close()


async def _submit_guard_close(db, pos: Position, price: float, reason: str,
                              source: str, dedup_key: str | None = None) -> dict | None:
    """守护平仓通用路径：多头卖出 / 空头买回；在途同向单不重复触发。"""
    from app.execution.order_manager import get_order_manager
    from app.notify.dispatcher import get_dispatcher

    close_side = OrderSide.SELL if pos.qty > 0 else OrderSide.BUY
    open_dup = db.scalar(select(Order).where(
        Order.broker == pos.broker, Order.symbol == pos.symbol,
        Order.side == close_side, Order.status.in_(_OPEN_STATUSES)))
    if open_dup is not None:
        return None

    sig = Signal(source=source,
                 dedup_key=dedup_key or f"guard:{pos.broker}:{pos.symbol}:{pos.id}:{price:.4f}",
                 symbol=pos.symbol, market=pos.market, action=close_side,
                 quantity=abs(pos.qty), order_type="market", price=price,
                 status=SignalStatus.RECEIVED, reject_reason=reason[:300])
    db.add(sig)
    try:
        db.commit()
    except Exception:
        db.rollback()  # dedup_key 冲突 = 本轮已处理过（到期守护幂等）
        return None
    db.refresh(sig)

    intent = OrderIntent(symbol=pos.symbol, market=Market(pos.market),
                         side=close_side, order_type=OrderType.MARKET,
                         qty=abs(pos.qty), est_price=price, broker=pos.broker,
                         strategy=source, multiplier=pos.multiplier or 1.0)
    order = await get_order_manager().submit(intent, signal_id=sig.id)
    sig.status = "failed" if order.status == "failed" else SignalStatus.ROUTED
    db.commit()
    action_cn = "卖出平仓" if close_side == OrderSide.SELL else "买回平仓"
    await get_dispatcher().emit(NotifyEvent(
        level=NotifyLevel.WARN,
        title="持仓守护平仓" if source == "risk_guard" else "期权到期自动平仓",
        body=f"{reason}（{action_cn}）",
        fields={"标的": pos.symbol, "账户": pos.broker,
                "数量": abs(pos.qty), "订单": f"#{order.id}"},
        broker=pos.broker))
    logger.warning("守护平仓 %s（%s）：%s", pos.symbol, pos.broker, reason)
    return {"symbol": pos.symbol, "broker": pos.broker, "reason": reason,
            "order_id": order.id}


async def run_expiry_guard() -> list[dict]:
    """期权到期守护：临近到期提醒（每日一次）+ 可选到期前 1 日自动平仓。"""
    from datetime import datetime, timezone

    from app.db.models import AppSetting
    from app.domain.contracts import days_to_expiry, is_option
    from app.notify.dispatcher import get_dispatcher

    db = SessionLocal()
    actions: list[dict] = []
    try:
        cfg = db.get(RiskConfig, 1)
        if cfg is None:
            return []
        today = datetime.now(timezone.utc).date().isoformat()
        notified_key = f"expiry_notified:{today}"
        row = db.get(AppSetting, notified_key)
        already = set(row.value or []) if row else set()

        positions = [p for p in db.scalars(select(Position).where(Position.qty != 0)).all()
                     if is_option(p.symbol)]
        for pos in positions:
            dte = days_to_expiry(pos.symbol)
            if dte is None:
                continue
            # 自动平仓：到期前 1 日（含到期日）
            if cfg.auto_close_before_expiry and dte <= 1 and cfg.trading_enabled:
                price = await _latest_price(pos.broker, pos.symbol)
                result = await _submit_guard_close(
                    db, pos, price or pos.avg_cost,
                    f"期权 {pos.symbol} 剩余 {dte} 天到期，自动平仓",
                    source="expiry_guard",
                    dedup_key=f"expiry:{pos.broker}:{pos.symbol}:{today}")
                if result is not None:
                    actions.append(result)
                continue
            # 提醒：到期前 N 天，每日一次
            if 0 <= dte <= cfg.expiry_warn_days and pos.symbol not in already:
                await get_dispatcher().emit(NotifyEvent(
                    level=NotifyLevel.WARN, title="期权临近到期",
                    body=f"{pos.symbol} 还有 {dte} 天到期，请及时处理（平仓/移仓）",
                    fields={"账户": pos.broker, "持仓": pos.qty,
                            "成本": pos.avg_cost},
                    broker=pos.broker))
                already.add(pos.symbol)
                actions.append({"symbol": pos.symbol, "broker": pos.broker,
                                "reason": f"到期提醒 dte={dte}", "order_id": None})

        if row is None:
            db.add(AppSetting(key=notified_key, value=sorted(already)))
        else:
            row.value = sorted(already)
        db.commit()
        return actions
    finally:
        db.close()
