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
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    trailing_stop_pct: float | None = None


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
    # 路由过滤：空列表 = 不限制；系统级事件（无策略/账户元数据）始终投递
    config: dict = {}


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


# ---------- 券商账户 ----------


class BrokerAccountBody(BaseModel):
    name: str
    type: str  # paper | futu | ibkr
    params: dict = {}
    enabled: bool = True


@router.get("/broker-accounts")
def list_broker_accounts(db: Session = Depends(get_db)):
    from app.db.models import BrokerAccount

    manager = get_broker_manager()
    status = manager.status()
    out = []
    for a in db.scalars(select(BrokerAccount).order_by(BrokerAccount.id)).all():
        row = _row(a)
        row.update(status.get(a.name, {"connected": False, "error": None}))
        out.append(row)
    return out


@router.post("/broker-accounts")
async def create_broker_account(body: BrokerAccountBody, db: Session = Depends(get_db)):
    from app.db.models import BrokerAccount

    if body.type not in ("paper", "futu", "ibkr"):
        raise HTTPException(400, "type 必须是 paper/futu/ibkr")
    if not body.name.strip() or len(body.name) > 32:
        raise HTTPException(400, "账户名不合法")
    if db.scalar(select(BrokerAccount).where(BrokerAccount.name == body.name)):
        raise HTTPException(400, f"账户名 {body.name} 已存在")
    account = BrokerAccount(**body.model_dump())
    db.add(account)
    db.commit()
    db.refresh(account)
    if account.enabled:
        manager = get_broker_manager()
        try:
            adapter = await manager.add_account(account.type, account.name, account.params)
            get_order_manager().attach_adapter(adapter)
        except Exception as e:
            raise HTTPException(400, f"账户已保存但注册失败: {e}")
    return _row(account)


@router.delete("/broker-accounts/{account_id}")
async def delete_broker_account(account_id: int, db: Session = Depends(get_db)):
    from app.db.models import BrokerAccount, Position as Pos

    account = db.get(BrokerAccount, account_id)
    if account is None:
        return {"ok": True}
    live_pos = db.scalar(select(func.count(Pos.id)).where(Pos.broker == account.name,
                                                          Pos.qty != 0)) or 0
    if live_pos:
        raise HTTPException(400, f"账户 {account.name} 仍有 {live_pos} 笔持仓，请先平仓再删除")
    await get_broker_manager().remove_account(account.name)
    db.delete(account)
    db.commit()
    return {"ok": True}


@router.post("/broker-accounts/{account_id}/toggle")
async def toggle_broker_account(account_id: int, db: Session = Depends(get_db)):
    from app.db.models import BrokerAccount

    account = db.get(BrokerAccount, account_id)
    if account is None:
        raise HTTPException(404, "账户不存在")
    account.enabled = not account.enabled
    db.commit()
    manager = get_broker_manager()
    if account.enabled:
        adapter = await manager.add_account(account.type, account.name, account.params)
        get_order_manager().attach_adapter(adapter)
    else:
        await manager.remove_account(account.name)
    return _row(account)


# ---------- 手动下单 ----------


class ManualOrderBody(BaseModel):
    broker: str
    symbol: str  # 内部格式：US.AAPL / HK.00700 / SH.600519
    side: str  # buy | sell
    order_type: str = "market"
    qty: float
    limit_price: float | None = None


@router.post("/manual-order")
async def manual_order(body: ManualOrderBody, db: Session = Depends(get_db)):
    """手动下单：与信号同样经过风控闸门，并以 Signal(source=manual) 留痕。"""
    import uuid

    from app.domain.enums import Market, OrderSide, OrderType, SignalStatus
    from app.domain.schemas import OrderIntent
    from app.risk.engine import get_risk_engine

    prefix = body.symbol.split(".", 1)[0]
    market = {"US": Market.US, "HK": Market.HK, "SH": Market.CN, "SZ": Market.CN}.get(prefix)
    if market is None:
        raise HTTPException(400, "symbol 必须是内部格式，如 US.AAPL / HK.00700 / SH.600519")
    try:
        side = OrderSide(body.side)
        order_type = OrderType(body.order_type)
    except ValueError:
        raise HTTPException(400, "side 必须是 buy/sell，order_type 必须是 market/limit")
    if body.qty <= 0:
        raise HTTPException(400, "qty 必须为正数")
    if order_type == OrderType.LIMIT and body.limit_price is None:
        raise HTTPException(400, "限价单必须提供 limit_price")

    est_price = body.limit_price
    if est_price is None:
        adapter = get_broker_manager().get_if_connected(body.broker)
        if adapter is not None:
            quote = await adapter.get_quote(body.symbol)
            if quote is not None:
                est_price = quote.price
        if est_price is None:
            import asyncio as aio

            from app.data.store import get_bar_store

            est_price = await aio.to_thread(get_bar_store().last_close, body.symbol)

    sig = Signal(source="manual", dedup_key=f"manual:{uuid.uuid4().hex}",
                 symbol=body.symbol, market=market, action=body.side,
                 quantity=body.qty, order_type=body.order_type, price=est_price,
                 status=SignalStatus.RECEIVED)
    db.add(sig)
    db.commit()
    db.refresh(sig)

    intent = OrderIntent(symbol=body.symbol, market=market, side=side,
                         order_type=order_type, qty=body.qty, est_price=est_price,
                         broker=body.broker, strategy="manual")
    decision = get_risk_engine().check(db, intent)
    if not decision.allowed:
        sig.status = SignalStatus.REJECTED_RISK
        sig.reject_reason = f"[{decision.rule_name}] {decision.reason}"[:300]
        db.commit()
        raise HTTPException(400, f"风控拦截：{decision.reason}")

    order = await get_order_manager().submit(intent, signal_id=sig.id)
    sig.status = "failed" if order.status == "failed" else "routed"
    sig.reject_reason = order.error_msg
    db.commit()
    if order.status == "failed":
        raise HTTPException(400, f"下单失败：{order.error_msg}")
    return {"signal_id": sig.id, "order_id": order.id, "status": order.status}


# ---------- 自选 watchlist ----------


class WatchlistBody(BaseModel):
    symbols: list[str]


def _get_watchlist(db: Session) -> list[str]:
    from app.db.models import AppSetting

    row = db.get(AppSetting, "watchlist")
    return list(row.value or []) if row else []


@router.get("/watchlist")
async def get_watchlist(db: Session = Depends(get_db)):
    import asyncio as aio

    from app.data.store import get_bar_store

    symbols = _get_watchlist(db)
    store = get_bar_store()
    out = []
    for sym in symbols:
        price = await aio.to_thread(store.last_close, sym)
        out.append({"symbol": sym, "last_close": price})
    return out


@router.put("/watchlist")
def set_watchlist(body: WatchlistBody, db: Session = Depends(get_db)):
    from app.db.models import AppSetting

    row = db.get(AppSetting, "watchlist")
    if row is None:
        row = AppSetting(key="watchlist", value=[])
        db.add(row)
    row.value = list(dict.fromkeys(body.symbols))[:200]
    db.commit()
    return {"watchlist": row.value}


@router.delete("/watchlist/{symbol}")
def remove_from_watchlist(symbol: str, db: Session = Depends(get_db)):
    from app.db.models import AppSetting

    row = db.get(AppSetting, "watchlist")
    if row is not None:
        row.value = [s for s in (row.value or []) if s != symbol]
        db.commit()
    return {"watchlist": row.value if row else []}


# ---------- 审计日志 ----------


@router.get("/audit-logs")
def list_audit_logs(page: int = 1, size: int = 20, db: Session = Depends(get_db)):
    from app.db.models import AuditLog

    stmt = select(AuditLog).order_by(AuditLog.id.desc())
    result = _paginate(db, stmt, page, size)
    result["items"] = [_row(a) for a in result["items"]]
    return result


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
