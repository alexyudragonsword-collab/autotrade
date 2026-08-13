"""信号 / 订单 / 持仓 / 风控 / 通知 / 券商 / 设置 等资源 API。"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.bootstrap import get_webhook_token, rotate_webhook_token
from app.brokers.manager import get_broker_manager
from app.db.models import (
    NotifyChannel,
    Order,
    Position,
    RiskConfig,
    RiskEventLog,
    Signal,
    TradeFill,
)
from app.execution.order_manager import get_order_manager
from app.notify.dispatcher import get_dispatcher

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["resources"], dependencies=[Depends(get_current_user)])


def _paginate(db: Session, stmt, page: int, size: int):
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.limit(size).offset((page - 1) * size)).all()
    return {"total": total, "page": page, "size": size, "items": rows}


def _row(obj) -> dict:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


# ---------- 信号 ----------


@router.get("/signals")
def list_signals(status: str | None = None, page: int = 1, size: int = 20,
                 db: Session = Depends(get_db)):
    stmt = select(Signal).order_by(Signal.id.desc())
    if status:
        stmt = stmt.where(Signal.status == status)
    result = _paginate(db, stmt, page, size)
    result["items"] = [_row(s) for s in result["items"]]
    return result


@router.get("/signals/{signal_id}")
def get_signal(signal_id: int, db: Session = Depends(get_db)):
    sig = db.get(Signal, signal_id)
    if sig is None:
        raise HTTPException(404, "信号不存在")
    data = _row(sig)
    orders = db.scalars(select(Order).where(Order.signal_id == signal_id)).all()
    data["orders"] = [_row(o) for o in orders]
    return data


# ---------- 订单 / 成交 / 持仓 ----------


@router.get("/orders")
def list_orders(status: str | None = None, broker: str | None = None,
                page: int = 1, size: int = 20, db: Session = Depends(get_db)):
    stmt = select(Order).order_by(Order.id.desc())
    if status:
        stmt = stmt.where(Order.status == status)
    if broker:
        stmt = stmt.where(Order.broker == broker)
    result = _paginate(db, stmt, page, size)
    result["items"] = [_row(o) for o in result["items"]]
    return result


@router.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: int):
    try:
        await get_order_manager().cancel(order_id)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.get("/orders/{order_id}/fills")
def list_fills(order_id: int, db: Session = Depends(get_db)):
    fills = db.scalars(select(TradeFill).where(TradeFill.order_id == order_id)).all()
    return [_row(f) for f in fills]


@router.get("/positions")
async def list_positions(db: Session = Depends(get_db)):
    rows = db.scalars(select(Position).where(Position.qty != 0)).all()
    out = []
    manager = get_broker_manager()
    for p in rows:
        item = _row(p)
        adapter = manager.get_if_connected(p.broker)
        if adapter is not None:
            try:
                quote = await adapter.get_quote(p.symbol)
                if quote:
                    item["last_price"] = quote.price
                    item["unrealized_pnl"] = round((quote.price - p.avg_cost) * p.qty, 2)
            except Exception:
                pass
        out.append(item)
    return out


@router.post("/positions/sync")
async def sync_positions():
    await get_order_manager().sync_positions()
    return {"ok": True}


# ---------- 风控 ----------


class RiskUpdate(BaseModel):
    trading_enabled: bool | None = None
    max_order_value: float | None = None
    max_position_value_per_symbol: float | None = None
    max_total_exposure: float | None = None
    max_orders_per_day: int | None = None
    max_daily_loss: float | None = None
    symbol_whitelist: list[str] | None = None
    trading_hours_enabled: bool | None = None


@router.get("/risk")
def get_risk(db: Session = Depends(get_db)):
    cfg = db.get(RiskConfig, 1)
    return _row(cfg)


@router.put("/risk")
def update_risk(update: RiskUpdate, db: Session = Depends(get_db)):
    cfg = db.get(RiskConfig, 1)
    for key, value in update.model_dump(exclude_none=True).items():
        setattr(cfg, key, value)
    db.commit()
    return _row(cfg)


class KillSwitchBody(BaseModel):
    enabled: bool  # true = 允许交易


@router.post("/risk/kill-switch")
async def kill_switch(body: KillSwitchBody, db: Session = Depends(get_db)):
    cfg = db.get(RiskConfig, 1)
    cfg.trading_enabled = body.enabled
    db.commit()
    from app.domain.enums import NotifyLevel
    from app.domain.schemas import NotifyEvent

    state = "已恢复交易" if body.enabled else "已紧急停止全部交易"
    await get_dispatcher().emit(NotifyEvent(
        level=NotifyLevel.ERROR if not body.enabled else NotifyLevel.WARN,
        title="Kill Switch", body=state))
    return {"trading_enabled": cfg.trading_enabled}


@router.get("/risk/events")
def risk_events(page: int = 1, size: int = 20, db: Session = Depends(get_db)):
    stmt = select(RiskEventLog).where(RiskEventLog.decision == "block").order_by(RiskEventLog.id.desc())
    result = _paginate(db, stmt, page, size)
    result["items"] = [_row(e) for e in result["items"]]
    return result


# ---------- 通知渠道 ----------


class ChannelBody(BaseModel):
    type: str
    name: str = ""
    enabled: bool = True
    min_level: str = "info"


@router.get("/notify/channels")
def list_channels(db: Session = Depends(get_db)):
    return [_row(c) for c in db.scalars(select(NotifyChannel)).all()]


@router.post("/notify/channels")
def create_channel(body: ChannelBody, db: Session = Depends(get_db)):
    if body.type not in ("telegram", "email", "wecom", "dingtalk"):
        raise HTTPException(400, "type 必须是 telegram/email/wecom/dingtalk")
    ch = NotifyChannel(**body.model_dump())
    db.add(ch)
    db.commit()
    db.refresh(ch)
    return _row(ch)


@router.put("/notify/channels/{channel_id}")
def update_channel(channel_id: int, body: ChannelBody, db: Session = Depends(get_db)):
    ch = db.get(NotifyChannel, channel_id)
    if ch is None:
        raise HTTPException(404, "渠道不存在")
    for k, v in body.model_dump().items():
        setattr(ch, k, v)
    db.commit()
    return _row(ch)


@router.delete("/notify/channels/{channel_id}")
def delete_channel(channel_id: int, db: Session = Depends(get_db)):
    ch = db.get(NotifyChannel, channel_id)
    if ch:
        db.delete(ch)
        db.commit()
    return {"ok": True}


@router.post("/notify/channels/{channel_id}/test")
async def test_channel(channel_id: int, db: Session = Depends(get_db)):
    ch = db.get(NotifyChannel, channel_id)
    if ch is None:
        raise HTTPException(404, "渠道不存在")
    try:
        await get_dispatcher().test_channel(ch.type)
    except Exception as e:
        raise HTTPException(400, f"发送失败: {e}")
    return {"ok": True}


# ---------- 券商 ----------


@router.get("/brokers/status")
def brokers_status():
    return get_broker_manager().status()


@router.post("/brokers/{name}/reconnect")
async def reconnect_broker(name: str):
    try:
        await get_broker_manager().reconnect(name)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.get("/brokers/{name}/account")
async def broker_account(name: str):
    try:
        adapter = get_broker_manager().get(name)
        acc = await adapter.get_account()
        return {"cash": acc.cash, "net_value": acc.net_value, "buying_power": acc.buying_power}
    except Exception as e:
        raise HTTPException(400, str(e))


# ---------- 设置 ----------


@router.get("/settings")
def get_app_settings(db: Session = Depends(get_db)):
    from app.config import get_settings

    s = get_settings()
    return {
        "webhook_token": get_webhook_token(db),
        "webhook_path": f"/webhook/tradingview/{get_webhook_token(db)}",
        "tv_ip_allowlist_enabled": s.tv_ip_allowlist_enabled,
        "brokers": {
            "paper_enabled": s.paper_enabled,
            "futu_enabled": s.futu_enabled,
            "futu_trd_env": s.futu_trd_env,
            "ibkr_enabled": s.ibkr_enabled,
            "ibkr_port": s.ibkr_port,
        },
    }


@router.post("/settings/webhook-token/rotate")
def rotate_token(db: Session = Depends(get_db)):
    return {"webhook_token": rotate_webhook_token(db)}


# ---------- 仪表盘 ----------


@router.get("/dashboard/summary")
async def dashboard_summary(db: Session = Depends(get_db)):
    from datetime import datetime, time, timezone

    day_start = datetime.combine(datetime.now(timezone.utc).date(), time.min, tzinfo=timezone.utc)
    cfg = db.get(RiskConfig, 1)
    manager = get_broker_manager()

    accounts = {}
    for name in list(manager._adapters):
        adapter = manager.get_if_connected(name)
        if adapter is None:
            continue
        try:
            acc = await asyncio.wait_for(adapter.get_account(), timeout=5)
            accounts[name] = {"cash": acc.cash, "net_value": acc.net_value}
        except Exception:
            pass

    return {
        "trading_enabled": cfg.trading_enabled if cfg else False,
        "brokers": manager.status(),
        "accounts": accounts,
        "signals_today": db.scalar(select(func.count(Signal.id))
                                   .where(Signal.created_at >= day_start)) or 0,
        "orders_today": db.scalar(select(func.count(Order.id))
                                  .where(Order.created_at >= day_start)) or 0,
        "blocked_today": db.scalar(select(func.count(RiskEventLog.id))
                                   .where(RiskEventLog.decision == "block",
                                          RiskEventLog.ts >= day_start)) or 0,
        "recent_signals": [_row(s) for s in db.scalars(
            select(Signal).order_by(Signal.id.desc()).limit(10)).all()],
        "recent_orders": [_row(o) for o in db.scalars(
            select(Order).order_by(Order.id.desc()).limit(10)).all()],
    }
