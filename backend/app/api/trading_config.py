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
    symbols: list[str] = []
    schedule_cron: str | None = None
    timeframe: str = "1d"
    notes: str | None = None


def _reschedule_strategy(cfg: StrategyConfig) -> None:
    try:
        from app.scheduler import schedule_strategy

        active = cfg.enabled and bool(cfg.class_name)
        schedule_strategy(cfg.id, cfg.schedule_cron if active else None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        logger.exception("更新策略 %s 定时任务失败", cfg.id)


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
    if body.class_name:
        from app.strategy.registry import get_strategy_class

        try:
            get_strategy_class(body.class_name)
        except ValueError as e:
            raise HTTPException(400, str(e))
    cfg = StrategyConfig(**body.model_dump())
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    _reschedule_strategy(cfg)
    return _row(cfg)


@router.put("/strategies/{strategy_id}")
def update_strategy(strategy_id: int, body: StrategyBody, db: Session = Depends(get_db)):
    cfg = db.get(StrategyConfig, strategy_id)
    if cfg is None:
        raise HTTPException(404, "策略不存在")
    for k, v in body.model_dump().items():
        setattr(cfg, k, v)
    db.commit()
    _reschedule_strategy(cfg)
    return _row(cfg)


@router.post("/strategies/{strategy_id}/toggle")
def toggle_strategy(strategy_id: int, db: Session = Depends(get_db)):
    cfg = db.get(StrategyConfig, strategy_id)
    if cfg is None:
        raise HTTPException(404, "策略不存在")
    cfg.enabled = not cfg.enabled
    db.commit()
    _reschedule_strategy(cfg)
    return _row(cfg)


@router.delete("/strategies/{strategy_id}")
def delete_strategy(strategy_id: int, db: Session = Depends(get_db)):
    cfg = db.get(StrategyConfig, strategy_id)
    if cfg:
        from app.scheduler import unschedule_strategy

        unschedule_strategy(cfg.id)
        db.delete(cfg)
        db.commit()
    return {"ok": True}


@router.post("/strategies/{strategy_id}/run-now")
async def run_strategy_now(strategy_id: int):
    """立即运行本地策略一次（拉最新行情 → on_bar → 信号入管道）。"""
    from app.strategy.live import run_strategy_live

    try:
        return await run_strategy_live(strategy_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------- 自定义策略（在线编辑器）----------


class CustomStrategyBody(BaseModel):
    class_name: str
    code: str
    enabled: bool = True


@router.get("/custom-strategies/template")
def custom_strategy_template():
    from app.strategy.custom import DEFAULT_TEMPLATE

    return {"template": DEFAULT_TEMPLATE}


@router.get("/custom-strategies")
def list_custom_strategy_rows(db: Session = Depends(get_db)):
    from app.db.models import CustomStrategy

    return [_row(s) for s in db.scalars(select(CustomStrategy).order_by(CustomStrategy.id)).all()]


@router.post("/custom-strategies/validate")
def validate_custom_strategy(body: CustomStrategyBody):
    from app.strategy.custom import StrategyCodeError, compile_strategy_code, validate_strategy_class

    try:
        cls = compile_strategy_code(body.code)
        report = validate_strategy_class(cls)
    except StrategyCodeError as e:
        raise HTTPException(400, str(e))
    report["detected_class"] = cls.__name__
    report["params"] = getattr(cls, "params", {})
    return report


def _save_custom(body: CustomStrategyBody, db: Session, existing_id: int | None = None):
    from app.db.models import CustomStrategy
    from app.strategy.custom import StrategyCodeError, compile_strategy_code, validate_strategy_class
    from app.strategy.registry import _REGISTRY

    try:
        cls = compile_strategy_code(body.code)
        validate_strategy_class(cls)
    except StrategyCodeError as e:
        raise HTTPException(400, str(e))
    if cls.__name__ != body.class_name:
        raise HTTPException(400, f"class_name 应与代码中的类名一致（检测到 {cls.__name__}）")
    if body.class_name in _REGISTRY:
        raise HTTPException(400, f"{body.class_name} 与内置策略同名，请换一个类名")

    if existing_id is not None:
        row = db.get(CustomStrategy, existing_id)
        if row is None:
            raise HTTPException(404, "自定义策略不存在")
        row.class_name = body.class_name
        row.code = body.code
        row.enabled = body.enabled
    else:
        if db.scalar(select(CustomStrategy).where(CustomStrategy.class_name == body.class_name)):
            raise HTTPException(400, f"{body.class_name} 已存在")
        row = CustomStrategy(**body.model_dump())
        db.add(row)
    db.commit()
    db.refresh(row)
    return _row(row)


@router.post("/custom-strategies")
def create_custom_strategy(body: CustomStrategyBody, db: Session = Depends(get_db)):
    return _save_custom(body, db)


@router.put("/custom-strategies/{strategy_id}")
def update_custom_strategy(strategy_id: int, body: CustomStrategyBody, db: Session = Depends(get_db)):
    return _save_custom(body, db, existing_id=strategy_id)


@router.delete("/custom-strategies/{strategy_id}")
def delete_custom_strategy(strategy_id: int, db: Session = Depends(get_db)):
    from app.db.models import CustomStrategy

    row = db.get(CustomStrategy, strategy_id)
    if row is not None:
        used = db.scalar(select(StrategyConfig).where(StrategyConfig.class_name == row.class_name))
        if used is not None:
            raise HTTPException(400, f"策略配置「{used.name}」仍在使用该类，请先解绑")
        db.delete(row)
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


class ResultBacktestBody(BaseModel):
    strategy_class: str
    params: dict = {}
    start_date: str
    end_date: str
    initial_cash: float = 100000.0
    top_n: int = 20  # 最多取前 N 只，避免一次回测几百只


@router.post("/screeners/{screener_id}/results/{result_id}/backtest")
def backtest_screen_result(screener_id: int, result_id: int, body: ResultBacktestBody,
                           db: Session = Depends(get_db)):
    """选股结果一键发起组合回测（结果内全部标的等权跑同一策略）。"""
    from app.strategy.registry import get_strategy_class

    result = db.get(ScreenResult, result_id)
    cfg = db.get(ScreenerConfig, screener_id)
    if result is None or cfg is None or result.screener_id != screener_id:
        raise HTTPException(404, "选股结果不存在")
    symbols = [r["symbol"] for r in (result.symbols or []) if r.get("symbol")][: body.top_n]
    if not symbols:
        raise HTTPException(400, "该结果没有可回测的标的")
    try:
        get_strategy_class(body.strategy_class)
    except ValueError as e:
        raise HTTPException(400, str(e))
    run = BacktestRun(
        strategy_class=body.strategy_class, params=body.params, symbols=symbols,
        market=cfg.market, start_date=body.start_date, end_date=body.end_date,
        initial_cash=body.initial_cash,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    submit_backtest(run.id)
    return _row(run)


@router.post("/screeners/{screener_id}/results/{result_id}/to-watchlist")
def screen_result_to_watchlist(screener_id: int, result_id: int, db: Session = Depends(get_db)):
    from app.db.models import AppSetting

    result = db.get(ScreenResult, result_id)
    if result is None or result.screener_id != screener_id:
        raise HTTPException(404, "选股结果不存在")
    symbols = [r["symbol"] for r in (result.symbols or []) if r.get("symbol")]
    row = db.get(AppSetting, "watchlist")
    if row is None:
        row = AppSetting(key="watchlist", value=[])
        db.add(row)
    merged = list(dict.fromkeys((row.value or []) + symbols))[:200]
    row.value = merged
    db.commit()
    return {"watchlist": merged, "added": len(symbols)}


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
    timeframe: str = "1d"
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


class BacktestScanBody(BaseModel):
    strategy_class: str
    param_grid: dict  # {"fast": [5,10,20], "slow": [30,60]} → 笛卡尔积逐组回测
    symbols: list[str]
    market: str = "US"
    start_date: str
    end_date: str
    timeframe: str = "1d"
    initial_cash: float = 100000.0

_MAX_SCAN_COMBOS = 60


def expand_param_grid(grid: dict) -> list[dict]:
    """{"a":[1,2],"b":[3]} → [{"a":1,"b":3},{"a":2,"b":3}]；标量视为单值列表。"""
    import itertools

    keys = list(grid.keys())
    value_lists = [v if isinstance(v, list) else [v] for v in grid.values()]
    return [dict(zip(keys, combo)) for combo in itertools.product(*value_lists)]


@router.post("/backtests/scan")
def create_backtest_scan(body: BacktestScanBody, db: Session = Depends(get_db)):
    """参数网格扫描：每组参数一个回测，同一 group_id，前端可对比寻优。"""
    import uuid

    from app.strategy.registry import get_strategy_class

    try:
        get_strategy_class(body.strategy_class)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not body.symbols:
        raise HTTPException(400, "至少选择一个标的")
    combos = expand_param_grid(body.param_grid or {})
    if not combos:
        raise HTTPException(400, "param_grid 不能为空")
    if len(combos) > _MAX_SCAN_COMBOS:
        raise HTTPException(400, f"参数组合 {len(combos)} 个，超过上限 {_MAX_SCAN_COMBOS}")

    group_id = uuid.uuid4().hex[:12]
    run_ids = []
    for params in combos:
        run = BacktestRun(
            strategy_class=body.strategy_class, params=params, symbols=body.symbols,
            market=body.market, start_date=body.start_date, end_date=body.end_date,
            timeframe=body.timeframe, initial_cash=body.initial_cash, group_id=group_id,
        )
        db.add(run)
        db.flush()
        run_ids.append(run.id)
    db.commit()
    for rid in run_ids:
        submit_backtest(rid)
    return {"group_id": group_id, "count": len(run_ids), "run_ids": run_ids}


@router.get("/backtests/groups/{group_id}")
def get_scan_group(group_id: str, db: Session = Depends(get_db)):
    """扫描组对比：按夏普降序返回各组合的参数与指标。"""
    runs = db.scalars(select(BacktestRun).where(BacktestRun.group_id == group_id)).all()
    if not runs:
        raise HTTPException(404, "扫描组不存在")
    items = []
    for r in runs:
        items.append({
            "id": r.id, "params": r.params, "status": r.status,
            "progress": r.progress, "metrics": r.metrics,
        })
    done = [i for i in items if i["metrics"]]
    pending = [i for i in items if not i["metrics"]]
    done.sort(key=lambda i: i["metrics"].get("sharpe", -999), reverse=True)
    return {"group_id": group_id, "total": len(items), "finished": len(done),
            "items": done + pending}


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


@router.get("/backtests/{run_id}/chart")
def backtest_chart(run_id: int, symbol: str, db: Session = Depends(get_db)):
    """单标的 K 线 + 该回测在此标的上的逐笔买卖点（走查复盘用）。"""
    from app.backtest.engine import ts_label
    from app.data.store import get_bar_store
    from app.domain.enums import Market

    run = db.get(BacktestRun, run_id)
    if run is None:
        raise HTTPException(404, "回测不存在")
    if symbol not in (run.symbols or []):
        raise HTTPException(400, f"{symbol} 不在该回测的标的列表中")
    bars = get_bar_store().get_bars(Market(run.market), symbol, run.start_date, run.end_date,
                                    refresh=False, interval=run.timeframe or "1d")
    if bars.empty:
        raise HTTPException(400, "本地无该标的行情缓存")
    kline = [[ts_label(ts), round(float(r["open"]), 4), round(float(r["close"]), 4),
              round(float(r["low"]), 4), round(float(r["high"]), 4), float(r["volume"])]
             for ts, r in bars.iterrows()]
    trades = [t for t in (run.trades or []) if t.get("symbol") == symbol]
    return {"symbol": symbol, "kline": kline, "trades": trades}
