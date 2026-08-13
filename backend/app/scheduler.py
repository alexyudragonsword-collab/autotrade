"""APScheduler 调度：券商健康检查、订单对账、持仓同步、限价单检查、选股定时任务。"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.db.base import SessionLocal

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


async def _health_check():
    from app.brokers.manager import get_broker_manager

    await get_broker_manager().health_check()


async def _reconcile_orders():
    from app.execution.order_manager import get_order_manager

    await get_order_manager().reconcile_stale()


async def _sync_positions():
    from app.execution.order_manager import get_order_manager

    await get_order_manager().sync_positions()


async def _check_paper_limits():
    from app.brokers.manager import get_broker_manager

    adapter = get_broker_manager().get_if_connected("paper")
    if adapter is not None:
        await adapter.check_pending_limits()


async def _run_screener_job(screener_id: int):
    import asyncio

    from app.screener.engine import ScreenerEngine

    def _run():
        db = SessionLocal()
        try:
            return ScreenerEngine().run(db, screener_id)
        finally:
            db.close()

    result = await asyncio.to_thread(_run)
    if result.error_msg is None and result.count:
        from app.domain.enums import NotifyLevel
        from app.domain.schemas import NotifyEvent
        from app.notify.dispatcher import get_dispatcher

        symbols = [r.get("symbol") for r in (result.symbols or [])][:30]
        await get_dispatcher().emit(NotifyEvent(
            level=NotifyLevel.INFO, title="定时选股结果",
            body=f"选出 {result.count} 只：\n" + "\n".join(symbols)))


def schedule_screener(screener_id: int, cron: str | None) -> None:
    scheduler = get_scheduler()
    job_id = f"screener_{screener_id}"
    existing = scheduler.get_job(job_id)
    if existing:
        existing.remove()
    if cron:
        try:
            trigger = CronTrigger.from_crontab(cron, timezone="Asia/Shanghai")
        except ValueError as e:
            raise ValueError(f"cron 表达式不合法: {e}")
        scheduler.add_job(_run_screener_job, trigger, args=[screener_id], id=job_id)
        logger.info("已注册选股器 %s 定时任务: %s", screener_id, cron)


def unschedule_screener(screener_id: int) -> None:
    job = get_scheduler().get_job(f"screener_{screener_id}")
    if job:
        job.remove()


def start_scheduler() -> None:
    from app.db.models import ScreenerConfig

    scheduler = get_scheduler()
    scheduler.add_job(_health_check, "interval", seconds=30, id="broker_health")
    scheduler.add_job(_reconcile_orders, "interval", seconds=60, id="order_reconcile")
    scheduler.add_job(_sync_positions, "interval", seconds=60, id="position_sync")
    scheduler.add_job(_check_paper_limits, "interval", seconds=20, id="paper_limits")

    db = SessionLocal()
    try:
        for cfg in db.scalars(select(ScreenerConfig).where(ScreenerConfig.enabled)).all():
            if cfg.schedule_cron:
                try:
                    schedule_screener(cfg.id, cfg.schedule_cron)
                except ValueError:
                    logger.warning("选股器 %s 的 cron 不合法，跳过", cfg.id)
    finally:
        db.close()

    scheduler.start()
    logger.info("调度器已启动")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
