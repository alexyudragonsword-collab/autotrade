"""迭代2：本地策略实盘驱动、成交盈亏、参数扫描、自选、审计。"""

import asyncio
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

import app.brokers.manager as manager_mod
import app.execution.order_manager as om_mod
import app.notify.dispatcher as dispatcher_mod
import app.risk.engine as risk_mod
import app.signals.pipeline as pipeline_mod
import app.strategy.registry as registry_mod
from app.brokers.manager import BrokerManager
from app.brokers.paper import PaperBroker
from app.db.models import Order, Position, Signal, StrategyConfig, TradeFill
from app.execution.order_manager import OrderManager
from app.risk.engine import RiskEngine
from app.risk.rules import default_rules
from app.signals.pipeline import SignalPipeline
from app.strategy.base import Strategy


class AlwaysBuy(Strategy):
    """测试用：空仓即买。"""

    params = {"qty": 7}

    def on_bar(self, ctx):
        if ctx.position() == 0:
            ctx.buy(self.p["qty"])


class MockDispatcher:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


@pytest.fixture
async def env(seeded, monkeypatch):
    price_holder = {"p": 100.0}

    async def quote(symbol):
        return price_holder["p"]

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
    monkeypatch.setattr(pipeline_mod, "_pipeline", SignalPipeline())
    monkeypatch.setitem(registry_mod._REGISTRY, "AlwaysBuy", AlwaysBuy)
    return {"price": price_holder, "broker": broker}


def _bars(closes):
    n = len(closes)
    return pd.DataFrame({
        "open": closes, "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes], "close": closes,
        "volume": [1000] * n,
    }, index=pd.bdate_range("2026-01-01", periods=n))


@pytest.fixture
def fake_store(tmp_path, monkeypatch):
    from app.data.store import BarStore
    import app.strategy.live as live_mod

    store = BarStore(base_dir=tmp_path)
    store.save("US.TEST", _bars(list(np.linspace(90, 100, 60))))

    class FrozenStore(BarStore):
        def get_bars(self, market, symbol, start, end, refresh=True):
            return super().get_bars(market, symbol, start, end, refresh=False)

    frozen = FrozenStore(base_dir=tmp_path)
    monkeypatch.setattr(live_mod, "get_bar_store", lambda: frozen)
    return frozen


async def test_live_strategy_generates_order(env, fake_store, seeded):
    seeded.add(StrategyConfig(name="local_test", class_name="AlwaysBuy",
                              params={"qty": 7}, enabled=True, mode="live",
                              broker="paper", symbols=["US.TEST"]))
    seeded.commit()
    from app.strategy.live import run_strategy_live

    cfg_id = seeded.scalar(select(StrategyConfig.id).where(StrategyConfig.name == "local_test"))
    summary = await run_strategy_live(cfg_id)
    await asyncio.sleep(0.2)
    assert summary["signals"] == 1
    assert summary["errors"] == []

    sig = seeded.scalar(select(Signal).where(Signal.strategy_name == "local_test"))
    assert sig is not None
    assert sig.source == "strategy"
    order = seeded.scalar(select(Order).where(Order.signal_id == sig.id))
    assert order.status == "filled"
    assert order.qty == 7

    # 重复运行：已持仓 → 策略本身不再发信号；即便发了，管道 dedup_key 也会拦
    summary2 = await run_strategy_live(cfg_id)
    await asyncio.sleep(0.2)
    assert summary2["signals"] == 0
    assert len(seeded.scalars(select(Order)).all()) == 1

    # 强行清持仓再跑（模拟同一 bar 内重复触发）→ dedup 兜底，仍只有 1 单
    seeded.query(Position).delete()
    seeded.commit()
    summary3 = await run_strategy_live(cfg_id)
    await asyncio.sleep(0.2)
    assert summary3["signals"] == 1  # 信号被 emit
    assert len(seeded.scalars(select(Order)).all()) == 1  # 但被幂等去重，没有新订单


async def test_realized_pnl_on_sell_fill(env, seeded):
    from app.domain.enums import Market, OrderSide, OrderType
    from app.domain.schemas import OrderIntent

    om = om_mod.get_order_manager()
    buy = OrderIntent(symbol="US.PNL", market=Market.US, side=OrderSide.BUY,
                      order_type=OrderType.MARKET, qty=10, est_price=100.0, broker="paper")
    await om.submit(buy)
    await asyncio.sleep(0.1)
    env["price"]["p"] = 110.0
    sell = OrderIntent(symbol="US.PNL", market=Market.US, side=OrderSide.SELL,
                       order_type=OrderType.MARKET, qty=4, est_price=110.0, broker="paper")
    await om.submit(sell)
    await asyncio.sleep(0.1)

    fills = seeded.scalars(select(TradeFill).order_by(TradeFill.id)).all()
    assert fills[0].realized_pnl is None  # 买入不计
    assert fills[1].realized_pnl == pytest.approx(40.0)  # (110-100)*4

    # 风控日亏损上下文应读取该值
    from app.domain.schemas import OrderIntent as OI

    engine = risk_mod.get_risk_engine()
    ctx = engine._build_context(seeded, OI(symbol="US.PNL", market=Market.US,
                                           side=OrderSide.BUY, order_type=OrderType.MARKET,
                                           qty=1, est_price=100.0, broker="paper"))
    assert ctx.realized_pnl_today == pytest.approx(40.0)


def test_expand_param_grid():
    from app.api.trading_config import expand_param_grid

    combos = expand_param_grid({"fast": [5, 10], "slow": [30, 60], "qty": 100})
    assert len(combos) == 4
    assert {"fast": 5, "slow": 30, "qty": 100} in combos
    assert {"fast": 10, "slow": 60, "qty": 100} in combos
    assert expand_param_grid({}) == [{}]


@pytest.fixture
async def client(seeded):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        c.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
        yield c


async def test_watchlist_api(client, seeded):
    resp = await client.put("/api/watchlist", json={"symbols": ["US.AAPL", "SH.600519", "US.AAPL"]})
    assert resp.json()["watchlist"] == ["US.AAPL", "SH.600519"]  # 去重保序
    resp = await client.delete("/api/watchlist/US.AAPL")
    assert resp.json()["watchlist"] == ["SH.600519"]
    resp = await client.get("/api/watchlist")
    assert resp.json()[0]["symbol"] == "SH.600519"


async def test_audit_log_records_writes(client, seeded):
    await client.put("/api/watchlist", json={"symbols": ["US.TSLA"]})
    resp = await client.get("/api/audit-logs")
    items = resp.json()["items"]
    assert any(i["path"] == "/api/watchlist" and i["method"] == "PUT"
               and i["username"] == "admin" for i in items)
    # 登录不入审计（不落密码）
    assert not any(i["path"] == "/api/auth/login" for i in items)


async def test_strategy_symbols_cron_roundtrip(client, seeded):
    resp = await client.post("/api/strategies", json={
        "name": "with_cron", "class_name": "SmaCross", "mode": "signal_only",
        "broker": "paper", "symbols": ["US.AAPL"], "schedule_cron": "0 16 * * 1-5",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["symbols"] == ["US.AAPL"]
    assert data["schedule_cron"] == "0 16 * * 1-5"
    # 非法 cron 被拒绝
    resp = await client.post("/api/strategies", json={
        "name": "bad_cron", "class_name": "SmaCross", "schedule_cron": "not-a-cron",
    })
    assert resp.status_code == 400
