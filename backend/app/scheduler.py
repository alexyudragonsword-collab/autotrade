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
    from app.brokers.paper import PaperBroker

    manager = get_broker_manager()
    for name in list(manager._adapters):
        adapter = manager.get_if_connected(name)
        if isinstance(adapter, PaperBroker):
            await adapter.check_pending_limits()


async def _account_snapshots():
    from app.api.performance import record_account_snapshots

    try:
        await record_account_snapshots()
    except Exception:
        logger.exception("账户净值快照失败")


async def _position_guard():
    from app.risk.guard import run_position_guard

    try:
        await run_position_guard()
    except Exception:
        logger.exception("持仓守护执行失败")


async def _expiry_guard():
    from app.risk.guard import run_expiry_guard

    try:
        await run_expiry_guard()
    except Exception:
        logger.exception("期权到期守护执行失败")


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


async def _run_strategy_job(strategy_id: int):
    from app.strategy.live import run_strategy_live

    try:
        summary = await run_strategy_live(strategy_id)
        logger.info("本地策略 %s 运行完成: %s", strategy_id, summary)
    except Exception:
        logger.exception("本地策略 %s 运行失败", strategy_id)


def schedule_strategy(strategy_id: int, cron: str | None) -> None:
    scheduler = get_scheduler()
    job_id = f"strategy_{strategy_id}"
    existing = scheduler.get_job(job_id)
    if existing:
        existing.remove()
    if cron:
        try:
            trigger = CronTrigger.from_crontab(cron, timezone="Asia/Shanghai")
        except ValueError as e:
            raise ValueError(f"cron 表达式不合法: {e}")
        scheduler.add_job(_run_strategy_job, trigger, args=[strategy_id], id=job_id)
        logger.info("已注册本地策略 %s 定时任务: %s", strategy_id, cron)


def unschedule_strategy(strategy_id: int) -> None:
    job = get_scheduler().get_job(f"strategy_{strategy_id}")
    if job:
        job.remove()


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


async def _backup_db():
    """SQLite 每日备份，保留最近 7 份（Postgres 用户请用 pg_dump，见文档）。"""
    import shutil
    from datetime import datetime, timezone
    from pathlib import Path

    from app.config import get_settings

    settings = get_settings()
    url = settings.database_url
    if not url.startswith("sqlite"):
        return
    db_path = Path(url.split("///", 1)[-1])
    if not db_path.exists():
        return
    backup_dir = settings.data_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    shutil.copy2(db_path, backup_dir / f"{db_path.stem}_{stamp}.db")
    backups = sorted(backup_dir.glob(f"{db_path.stem}_*.db"))
    for old in backups[:-7]:
        old.unlink(missing_ok=True)
    logger.info("数据库已备份（共 %d 份）", min(len(backups), 7))


def start_scheduler() -> None:
    from app.db.models import ScreenerConfig, StrategyConfig

    scheduler = get_scheduler()
    scheduler.add_job(_health_check, "interval", seconds=30, id="broker_health")
    scheduler.add_job(_reconcile_orders, "interval", seconds=60, id="order_reconcile")
    scheduler.add_job(_sync_positions, "interval", seconds=60, id="position_sync")
    scheduler.add_job(_check_paper_limits, "interval", seconds=20, id="paper_limits")
    scheduler.add_job(_position_guard, "interval", seconds=60, id="position_guard")
    scheduler.add_job(_expiry_guard, "interval", hours=4, id="expiry_guard")
    scheduler.add_job(_backup_db, CronTrigger.from_crontab("30 20 * * *", timezone="UTC"),
                      id="db_backup")
    scheduler.add_job(_account_snapshots, "interval", hours=4, id="account_snapshots")

    db = SessionLocal()
    try:
        for cfg in db.scalars(select(ScreenerConfig).where(ScreenerConfig.enabled)).all():
            if cfg.schedule_cron:
                try:
                    schedule_screener(cfg.id, cfg.schedule_cron)
                except ValueError:
                    logger.warning("选股器 %s 的 cron 不合法，跳过", cfg.id)
        for cfg in db.scalars(select(StrategyConfig).where(StrategyConfig.enabled)).all():
            if cfg.class_name and cfg.schedule_cron:
                try:
                    schedule_strategy(cfg.id, cfg.schedule_cron)
                except ValueError:
                    logger.warning("策略 %s 的 cron 不合法，跳过", cfg.id)
    finally:
        db.close()

    scheduler.start()
    logger.info("调度器已启动")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
