"""回测任务执行：线程池后台跑，进度/结果写回 BacktestRun。"""

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

from app.backtest.engine import BacktestEngine
from app.backtest.metrics import compute_metrics
from app.data.store import get_bar_store
from app.db.base import SessionLocal
from app.db.models import BacktestRun
from app.domain.enums import BacktestStatus, Market
from app.strategy.registry import get_strategy_class

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="backtest")


def submit_backtest(run_id: int) -> None:
    _executor.submit(_run, run_id)


def _run(run_id: int) -> None:
    db = SessionLocal()
    try:
        run = db.get(BacktestRun, run_id)
        if run is None:
            return
        run.status = BacktestStatus.RUNNING
        db.commit()

        strategy_cls = get_strategy_class(run.strategy_class)
        store = get_bar_store()
        market = Market(run.market)
        bars = {}
        for sym in run.symbols:
            df = store.get_bars(market, sym, run.start_date, run.end_date,
                                interval=run.timeframe or "1d")
            if df.empty:
                logger.warning("回测 %s：%s 无行情数据，跳过", run_id, sym)
            else:
                bars[sym] = df
        if not bars:
            raise ValueError("所有标的都没有行情数据，请检查代码/日期范围")

        def on_progress(p: float):
            run.progress = round(p, 3)
            db.commit()

        engine = BacktestEngine(
            strategy_cls, run.params, bars,
            initial_cash=run.initial_cash,
            commission_bps=run.commission_bps,
            slippage_bps=run.slippage_bps,
            progress_cb=on_progress,
        )
        result = engine.run()
        run.metrics = compute_metrics(result)

        # 等权买入持有基准与月度收益
        from app.backtest.metrics import benchmark_curve, monthly_returns

        bench = benchmark_curve(engine.bars, engine.calendar, run.initial_cash)
        curve = result.equity_curve
        if bench and len(bench) == len(curve):
            curve = [[d, e, b] for (d, e), b in zip(curve, bench)]
            bench_return = bench[-1] / bench[0] - 1 if bench[0] else 0.0
            run.metrics["benchmark_return"] = round(float(bench_return), 4)
            run.metrics["alpha"] = round(run.metrics["total_return"] - float(bench_return), 4)
        run.metrics["monthly_returns"] = monthly_returns(curve)
        run.equity_curve = curve
        run.trades = [asdict(t) for t in result.trades]
        run.status = BacktestStatus.DONE
        run.progress = 1.0
        db.commit()
        logger.info("回测 %s 完成: %s", run_id, run.metrics)
    except Exception as e:
        logger.exception("回测 %s 失败", run_id)
        db.rollback()
        run = db.get(BacktestRun, run_id)
        if run is not None:
            run.status = BacktestStatus.FAILED
            run.error_msg = str(e)[:2000]
            db.commit()
    finally:
        db.close()
