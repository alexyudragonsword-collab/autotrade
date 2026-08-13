"""迭代3：Telegram 指令、手动下单、回测基准与月度收益。"""

import asyncio
from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import select

import app.brokers.manager as manager_mod
import app.execution.order_manager as om_mod
import app.notify.dispatcher as dispatcher_mod
import app.risk.engine as risk_mod
from app.brokers.manager import BrokerManager
from app.brokers.paper import PaperBroker
from app.db.models import Order, Position, RiskConfig, Signal, StrategyConfig
from app.execution.order_manager import OrderManager
from app.notify.telegram_bot import TelegramBot
from app.risk.engine import RiskEngine
from app.risk.rules import default_rules


class MockDispatcher:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


@pytest.fixture
async def env(seeded, monkeypatch):
    async def quote(symbol):
        return 100.0

    broker = PaperBroker(quote_fn=quote, fee_bps=0)
    await broker.connect()
    manager = BrokerManager()
    manager.register(broker)
    order_manager = OrderManager(manager)
    order_manager.attach_callbacks()
    risk_engine = RiskEngine(rules=default_rules(
        now_fn=lambda: datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)))

    monkeypatch.setattr(manager_mod, "_manager", manager)
    monkeypatch.setattr(om_mod, "_order_manager", order_manager)
    monkeypatch.setattr(dispatcher_mod, "_dispatcher", MockDispatcher())
    monkeypatch.setattr(risk_mod, "_engine", risk_engine)
    return {"broker": broker}


@pytest.fixture
async def client(env, seeded):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        c.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
        yield c


# ---------- 手动下单 ----------


async def test_manual_order_fills(client, seeded):
    resp = await client.post("/api/manual-order", json={
        "broker": "paper", "symbol": "US.AAPL", "side": "buy",
        "order_type": "market", "qty": 5,
    })
    assert resp.status_code == 200
    data = resp.json()
    await asyncio.sleep(0.15)
    order = seeded.get(Order, data["order_id"])
    assert order.status == "filled"
    sig = seeded.get(Signal, data["signal_id"])
    assert sig.source == "manual"
    pos = seeded.scalar(select(Position).where(Position.symbol == "US.AAPL"))
    assert pos.qty == 5


async def test_manual_order_risk_blocked(client, seeded):
    cfg = seeded.get(RiskConfig, 1)
    cfg.trading_enabled = False
    seeded.commit()
    resp = await client.post("/api/manual-order", json={
        "broker": "paper", "symbol": "US.AAPL", "side": "buy",
        "order_type": "market", "qty": 5,
    })
    assert resp.status_code == 400
    assert "风控拦截" in resp.json()["detail"]
    sig = seeded.scalar(select(Signal).where(Signal.source == "manual"))
    assert sig.status == "rejected_risk"
    assert seeded.scalars(select(Order)).all() == []


async def test_manual_order_validation(client):
    resp = await client.post("/api/manual-order", json={
        "broker": "paper", "symbol": "AAPL", "side": "buy", "qty": 5,
    })
    assert resp.status_code == 400  # 非内部格式
    resp = await client.post("/api/manual-order", json={
        "broker": "paper", "symbol": "US.AAPL", "side": "buy",
        "order_type": "limit", "qty": 5,
    })
    assert resp.status_code == 400  # 限价缺价


# ---------- Telegram 指令 ----------


async def test_telegram_commands(env, seeded):
    bot = TelegramBot()
    assert "指令" in await bot.handle_command("/help")

    reply = await bot.handle_command("/status")
    assert "交易开关" in reply and "✅ 允许" in reply
    assert "paper" in reply

    assert await bot.handle_command("/pos") == "当前无持仓"
    assert await bot.handle_command("/orders") == "暂无订单"

    # kill / resume
    reply = await bot.handle_command("/kill")
    assert "停止" in reply
    assert seeded.get(RiskConfig, 1).trading_enabled is False
    seeded.expire_all()
    reply = await bot.handle_command("/resume")
    assert "恢复" in reply
    seeded.expire_all()
    assert seeded.get(RiskConfig, 1).trading_enabled is True

    # strategies + run
    seeded.add(StrategyConfig(name="tg_test", enabled=True, mode="signal_only", broker="paper"))
    seeded.commit()
    reply = await bot.handle_command("/strategies")
    assert "tg_test" in reply
    reply = await bot.handle_command("/run nonexistent")
    assert "不存在" in reply
    reply = await bot.handle_command("/run tg_test")
    assert "失败" in reply  # 未绑定本地策略类

    assert "未知指令" in await bot.handle_command("/bogus")


# ---------- 回测基准与月度收益 ----------


def _bars(closes, start="2026-01-01"):
    n = len(closes)
    return pd.DataFrame({
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1000] * n,
    }, index=pd.bdate_range(start, periods=n))


def test_benchmark_curve_and_alpha():
    from app.backtest.metrics import benchmark_curve

    bars = {"A": _bars([10.0, 11.0, 12.0]), "B": _bars([20.0, 20.0, 30.0])}
    calendar = bars["A"].index
    curve = benchmark_curve(bars, calendar, 1000.0)
    # A: 500/10=50股, B: 500/20=25股
    assert curve[0] == 1000.0
    assert curve[1] == 50 * 11 + 25 * 20  # 1050
    assert curve[2] == 50 * 12 + 25 * 30  # 1350


def test_monthly_returns():
    from app.backtest.metrics import monthly_returns

    curve = [["2026-01-05", 100.0], ["2026-01-30", 110.0],
             ["2026-02-10", 99.0], ["2026-02-27", 121.0]]
    rets = monthly_returns(curve)
    assert rets[0] == {"month": "2026-01", "ret": 0.1}
    assert rets[1] == {"month": "2026-02", "ret": 0.1}  # 121/110-1


def test_runner_attaches_benchmark(seeded, tmp_path, monkeypatch):
    import app.backtest.runner as runner_mod
    from app.backtest.runner import _run
    from app.data.store import BarStore
    from app.db.models import BacktestRun

    store = BarStore(base_dir=tmp_path)
    store.save("US.BM", _bars([100.0] * 30))

    class FrozenStore(BarStore):
        def get_bars(self, market, symbol, start, end, refresh=True):
            return super().get_bars(market, symbol, start, end, refresh=False)

    monkeypatch.setattr(runner_mod, "get_bar_store", lambda: FrozenStore(base_dir=tmp_path))

    run = BacktestRun(strategy_class="SmaCross", params={}, symbols=["US.BM"],
                      market="US", start_date="2026-01-01", end_date="2026-03-01",
                      initial_cash=10000.0)
    seeded.add(run)
    seeded.commit()
    _run(run.id)
    seeded.expire_all()
    run = seeded.get(BacktestRun, run.id)
    assert run.status == "done"
    assert run.metrics["benchmark_return"] == 0.0  # 平盘
    assert run.metrics["alpha"] == run.metrics["total_return"]
    assert len(run.equity_curve[0]) == 3  # [date, equity, benchmark]
    assert "monthly_returns" in run.metrics
