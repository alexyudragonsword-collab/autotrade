"""策略配置 / 选股器 / 回测 API。"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.backtest.runner import submit_backtest
from app.db.models import BacktestRun, ScreenerConfig, ScreenResult, StrategyConfig
from app.domain.enums import NotifyLevel
from app.domain.schemas import NotifyEvent
from app.notify.dispatcher import get_dispatcher
from app.screener.engine import ScreenerEngine, validate_rules
from app.screener.expressions import ExprError
from app.strategy.registry import list_strategies

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["trading-config"], dependencies=[Depends(get_current_user)])


def _row(obj) -> dict:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


# ---------- 策略配置 ----------


class StrategyBody(BaseModel):
    name: str
    class_name: str | None = None
    params: dict = {}
    enabled: bool = True
    mode: str = "signal_only"
    broker: str = "paper"
    default_qty: float = 0.0
    notes: str | None = None


@router.get("/strategies")
def list_strategy_configs(db: Session = Depends(get_db)):
    return [_row(s) for s in db.scalars(select(StrategyConfig).order_by(StrategyConfig.id)).all()]


@router.get("/strategies/builtin")
def builtin_strategies():
    return list_strategies()


@router.post("/strategies")
def create_strategy(body: StrategyBody, db: Session = Depends(get_db)):
    if body.mode not in ("signal_only", "live"):
        raise HTTPException(400, "mode 必须是 signal_only 或 live")
    if db.scalar(select(StrategyConfig).where(StrategyConfig.name == body.name)):
        raise HTTPException(400, f"策略名 {body.name} 已存在")
    cfg = StrategyConfig(**body.model_dump())
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return _row(cfg)


@router.put("/strategies/{strategy_id}")
def update_strategy(strategy_id: int, body: StrategyBody, db: Session = Depends(get_db)):
    cfg = db.get(StrategyConfig, strategy_id)
    if cfg is None:
        raise HTTPException(404, "策略不存在")
    for k, v in body.model_dump().items():
        setattr(cfg, k, v)
    db.commit()
    return _row(cfg)


@router.post("/strategies/{strategy_id}/toggle")
def toggle_strategy(strategy_id: int, db: Session = Depends(get_db)):
    cfg = db.get(StrategyConfig, strategy_id)
    if cfg is None:
        raise HTTPException(404, "策略不存在")
    cfg.enabled = not cfg.enabled
    db.commit()
    return _row(cfg)


@router.delete("/strategies/{strategy_id}")
def delete_strategy(strategy_id: int, db: Session = Depends(get_db)):
    cfg = db.get(StrategyConfig, strategy_id)
    if cfg:
        db.delete(cfg)
        db.commit()
    return {"ok": True}


# ---------- 选股器 ----------


class ScreenerBody(BaseModel):
    name: str
    market: str = "CN"
    universe: dict = {}
    rules: dict = {}
    schedule_cron: str | None = None
    enabled: bool = True


@router.get("/screeners")
def list_screeners(db: Session = Depends(get_db)):
    return [_row(s) for s in db.scalars(select(ScreenerConfig).order_by(ScreenerConfig.id)).all()]


@router.post("/screeners")
def create_screener(body: ScreenerBody, db: Session = Depends(get_db)):
    try:
        validate_rules(body.rules)
    except ExprError as e:
        raise HTTPException(400, f"规则不合法: {e}")
    cfg = ScreenerConfig(**body.model_dump())
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    _reschedule(cfg)
    return _row(cfg)


@router.put("/screeners/{screener_id}")
def update_screener(screener_id: int, body: ScreenerBody, db: Session = Depends(get_db)):
    cfg = db.get(ScreenerConfig, screener_id)
    if cfg is None:
        raise HTTPException(404, "选股器不存在")
    try:
        validate_rules(body.rules)
    except ExprError as e:
        raise HTTPException(400, f"规则不合法: {e}")
    for k, v in body.model_dump().items():
        setattr(cfg, k, v)
    db.commit()
    _reschedule(cfg)
    return _row(cfg)


@router.delete("/screeners/{screener_id}")
def delete_screener(screener_id: int, db: Session = Depends(get_db)):
    cfg = db.get(ScreenerConfig, screener_id)
    if cfg:
        from app.scheduler import unschedule_screener

        unschedule_screener(cfg.id)
        db.delete(cfg)
        db.commit()
    return {"ok": True}


def _reschedule(cfg: ScreenerConfig) -> None:
    try:
        from app.scheduler import schedule_screener

        schedule_screener(cfg.id, cfg.schedule_cron if cfg.enabled else None)
    except Exception:
        logger.exception("更新选股器 %s 定时任务失败", cfg.id)


@router.post("/screeners/{screener_id}/run")
async def run_screener(screener_id: int, db: Session = Depends(get_db)):
    import asyncio

    result = await asyncio.to_thread(_run_sync, screener_id)
    return _row(result)


def _run_sync(screener_id: int) -> ScreenResult:
    from app.db.base import SessionLocal

    db = SessionLocal()
    try:
        return ScreenerEngine().run(db, screener_id)
    finally:
        db.close()


@router.get("/screeners/{screener_id}/results")
def screener_results(screener_id: int, page: int = 1, size: int = 10, db: Session = Depends(get_db)):
    stmt = select(ScreenResult).where(ScreenResult.screener_id == screener_id) \
        .order_by(ScreenResult.id.desc())
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.limit(size).offset((page - 1) * size)).all()
    return {"total": total, "items": [_row(r) for r in rows]}


@router.post("/screeners/{screener_id}/results/{result_id}/notify")
async def notify_result(screener_id: int, result_id: int, db: Session = Depends(get_db)):
    result = db.get(ScreenResult, result_id)
    cfg = db.get(ScreenerConfig, screener_id)
    if result is None or cfg is None:
        raise HTTPException(404, "结果不存在")
    symbols = [r.get("symbol") for r in (result.symbols or [])][:30]
    await get_dispatcher().emit(NotifyEvent(
        level=NotifyLevel.INFO, title=f"选股结果：{cfg.name}",
        body=f"共 {result.count} 只：\n" + "\n".join(symbols)))
    return {"ok": True}


# ---------- 回测 ----------


class BacktestBody(BaseModel):
    strategy_class: str
    params: dict = {}
    symbols: list[str]
    market: str = "US"
    start_date: str
    end_date: str
    initial_cash: float = 100000.0
    commission_bps: float = 3.0
    slippage_bps: float = 1.0


@router.post("/backtests")
def create_backtest(body: BacktestBody, db: Session = Depends(get_db)):
    from app.strategy.registry import get_strategy_class

    try:
        get_strategy_class(body.strategy_class)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not body.symbols:
        raise HTTPException(400, "至少选择一个标的")
    run = BacktestRun(**body.model_dump())
    db.add(run)
    db.commit()
    db.refresh(run)
    submit_backtest(run.id)
    return _row(run)


@router.get("/backtests")
def list_backtests(page: int = 1, size: int = 10, db: Session = Depends(get_db)):
    stmt = select(BacktestRun).order_by(BacktestRun.id.desc())
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.limit(size).offset((page - 1) * size)).all()
    items = []
    for r in rows:
        item = _row(r)
        item.pop("equity_curve", None)  # 列表页不带大字段
        item.pop("trades", None)
        items.append(item)
    return {"total": total, "items": items}


@router.get("/backtests/{run_id}")
def get_backtest(run_id: int, db: Session = Depends(get_db)):
    run = db.get(BacktestRun, run_id)
    if run is None:
        raise HTTPException(404, "回测不存在")
    return _row(run)
