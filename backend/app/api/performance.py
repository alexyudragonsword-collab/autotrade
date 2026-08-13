"""实盘绩效归因 API：按策略/账户/标的统计已实现盈亏、账户净值曲线。"""

import logging
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.db.models import AccountValueSnapshot, Order, Signal, TradeFill

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/performance", tags=["performance"],
                   dependencies=[Depends(get_current_user)])


def _since(days: int) -> datetime:
    day = datetime.now(timezone.utc).date() - timedelta(days=days)
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


@router.get("/summary")
def performance_summary(days: int = 30, db: Session = Depends(get_db)):
    """已实现盈亏归因（基于成交时落库的 realized_pnl，卖出侧计）。"""
    since = _since(days)
    rows = db.execute(
        select(TradeFill, Order, Signal)
        .join(Order, TradeFill.order_id == Order.id)
        .outerjoin(Signal, Order.signal_id == Signal.id)
        .where(TradeFill.ts >= since)
    ).all()

    by_strategy: dict[str, dict] = {}
    by_symbol: dict[str, dict] = {}
    by_account: dict[str, dict] = {}
    daily: dict[str, float] = {}
    total_fees = 0.0

    for fill, order, signal in rows:
        total_fees += fill.fee or 0.0
        pnl = fill.realized_pnl
        strategy = (signal.strategy_name if signal and signal.strategy_name
                    else (signal.source if signal else "unknown"))
        for bucket, key in ((by_strategy, strategy), (by_symbol, order.symbol),
                            (by_account, order.broker)):
            item = bucket.setdefault(key, {"realized_pnl": 0.0, "closed_trades": 0,
                                           "wins": 0, "fills": 0, "fees": 0.0})
            item["fills"] += 1
            item["fees"] += fill.fee or 0.0
            if pnl is not None:
                item["realized_pnl"] += pnl
                item["closed_trades"] += 1
                if pnl > 0:
                    item["wins"] += 1
        if pnl is not None:
            day = fill.ts.date().isoformat() if fill.ts else date.today().isoformat()
            daily[day] = daily.get(day, 0.0) + pnl

    def _finalize(bucket: dict[str, dict]) -> list[dict]:
        out = []
        for key, item in bucket.items():
            closed = item["closed_trades"]
            out.append({
                "key": key,
                "realized_pnl": round(item["realized_pnl"], 2),
                "closed_trades": closed,
                "win_rate": round(item["wins"] / closed, 4) if closed else None,
                "fills": item["fills"],
                "fees": round(item["fees"], 2),
            })
        out.sort(key=lambda x: x["realized_pnl"], reverse=True)
        return out

    return {
        "days": days,
        "total_realized_pnl": round(sum(daily.values()), 2),
        "total_fees": round(total_fees, 2),
        "by_strategy": _finalize(by_strategy),
        "by_symbol": _finalize(by_symbol),
        "by_account": _finalize(by_account),
        "daily_pnl": sorted([[d, round(v, 2)] for d, v in daily.items()]),
    }


@router.get("/equity")
def equity_curves(days: int = 90, db: Session = Depends(get_db)):
    """各账户净值曲线（来自每日快照）。"""
    since_day = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    rows = db.scalars(
        select(AccountValueSnapshot)
        .where(AccountValueSnapshot.date >= since_day)
        .order_by(AccountValueSnapshot.date)
    ).all()
    curves: dict[str, list] = {}
    for r in rows:
        curves.setdefault(r.broker, []).append([r.date, round(r.net_value, 2)])
    return curves


@router.post("/snapshot")
async def snapshot_now():
    """立即为所有在线账户记一笔净值快照（同日 upsert）。"""
    count = await record_account_snapshots()
    return {"recorded": count}


async def record_account_snapshots() -> int:
    """由调度器每日调用 / API 手动触发。"""
    import asyncio

    from app.brokers.manager import get_broker_manager
    from app.db.base import SessionLocal

    manager = get_broker_manager()
    today = datetime.now(timezone.utc).date().isoformat()
    recorded = 0
    db = SessionLocal()
    try:
        for name in list(manager._adapters):
            adapter = manager.get_if_connected(name)
            if adapter is None:
                continue
            try:
                account = await asyncio.wait_for(adapter.get_account(), timeout=10)
            except Exception:
                logger.warning("快照获取账户 %s 失败", name)
                continue
            row = db.scalar(select(AccountValueSnapshot).where(
                AccountValueSnapshot.broker == name, AccountValueSnapshot.date == today))
            if row is None:
                row = AccountValueSnapshot(broker=name, date=today)
                db.add(row)
            row.cash = account.cash
            row.net_value = account.net_value
            recorded += 1
        db.commit()
    finally:
        db.close()
    return recorded
