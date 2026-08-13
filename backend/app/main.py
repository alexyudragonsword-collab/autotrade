import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth, performance, resources, trading_config
from app.bootstrap import seed_all
from app.db.base import SessionLocal, init_db
from app.logging_conf import setup_logging
from app.signals import webhook

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    init_db()
    db = SessionLocal()
    try:
        seed_all(db)
        from app.notify.dispatcher import seed_channels_from_env

        seed_channels_from_env(db)
    finally:
        db.close()

    from app.brokers.manager import get_broker_manager
    from app.execution.order_manager import get_order_manager
    from app.scheduler import start_scheduler, stop_scheduler

    from app.notify.telegram_bot import get_telegram_bot

    manager = get_broker_manager()
    get_order_manager()  # 注册回报回调
    await manager.connect_all()
    start_scheduler()
    bot = get_telegram_bot()
    bot.start()
    logger.info("AutoTrade 启动完成")
    try:
        yield
    finally:
        await bot.stop()
        stop_scheduler()
        await manager.disconnect_all()


def create_app() -> FastAPI:
    from app.audit import AuditMiddleware

    app = FastAPI(title="AutoTrade", version="0.2.0", lifespan=lifespan)
    app.add_middleware(AuditMiddleware)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    app.include_router(auth.router)
    app.include_router(resources.router)
    app.include_router(trading_config.router)
    app.include_router(performance.router)
    app.include_router(webhook.router)

    # 前端构建产物（frontend/dist）由后端托管；SPA 路由回退到 index.html
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            file = dist / full_path
            if full_path and file.is_file():
                return FileResponse(file)
            return FileResponse(dist / "index.html")

    return app


app = create_app()
