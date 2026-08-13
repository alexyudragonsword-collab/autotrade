"""端到端：TV webhook → 去重 → 风控 → PaperBroker 成交 → 通知（Mock）。"""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import app.brokers.manager as manager_mod
import app.execution.order_manager as om_mod
import app.notify.dispatcher as dispatcher_mod
import app.risk.engine as risk_mod
import app.signals.pipeline as pipeline_mod
from app.bootstrap import get_webhook_token, seed_all
from app.brokers.manager import BrokerManager
from app.brokers.paper import PaperBroker
from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models import Order, Position, Signal, StrategyConfig
from app.domain.schemas import NotifyEvent
from app.execution.order_manager import OrderManager
from app.risk.engine import RiskEngine
from app.risk.rules import default_rules
from app.signals.pipeline import SignalPipeline


class MockDispatcher:
    def __init__(self):
        self.events: list[NotifyEvent] = []

    async def emit(self, event):
        self.events.append(event)


@pytest.fixture
async def env(seeded, monkeypatch):
    """搭一套隔离的 manager/order_manager/pipeline/dispatcher。"""

    async def quote(symbol):
        return 100.0

    broker = PaperBroker(quote_fn=quote, fee_bps=0)
    await broker.connect()
    manager = BrokerManager()
    manager.register(broker)
    order_manager = OrderManager(manager)
    order_manager.attach_callbacks()
    dispatcher = MockDispatcher()
    # 风控放行时段（周三）
    from datetime import datetime, timezone

    risk_engine = RiskEngine(rules=default_rules(
        now_fn=lambda: datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)))

    monkeypatch.setattr(manager_mod, "_manager", manager)
    monkeypatch.setattr(om_mod, "_order_manager", order_manager)
    monkeypatch.setattr(dispatcher_mod, "_dispatcher", dispatcher)
    monkeypatch.setattr(risk_mod, "_engine", risk_engine)
    monkeypatch.setattr(pipeline_mod, "_pipeline", SignalPipeline())

    seeded.add(StrategyConfig(name="e2e", enabled=True, mode="live",
                              broker="paper", default_qty=5))
    seeded.commit()
    return {"dispatcher": dispatcher, "broker": broker}


def _payload(**overrides):
    base = {
        "secret": get_settings().webhook_secret,
        "alert_id": "e2e-001", "strategy": "e2e",
        "symbol": "AAPL", "exchange": "NASDAQ",
        "action": "buy", "qty": 10, "order_type": "market", "price": 100.0,
    }
    base.update(overrides)
    return base


@pytest.fixture
async def client(env):
    # 不触发 lifespan（env fixture 已手工搭好依赖）
    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _token():
    db = SessionLocal()
    try:
        return get_webhook_token(db)
    finally:
        db.close()


async def test_full_flow(client, env, seeded):
    resp = await client.post(f"/webhook/tradingview/{_token()}", json=_payload())
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    await asyncio.sleep(0.2)  # 等异步管道 + paper 成交

    sig = seeded.scalar(select(Signal).where(Signal.dedup_key == "tv:e2e-001"))
    assert sig is not None
    assert sig.status == "executed"

    order = seeded.scalar(select(Order).where(Order.signal_id == sig.id))
    assert order.status == "filled"
    assert order.filled_qty == 10
    assert order.avg_fill_price == 100.0

    pos = seeded.scalar(select(Position).where(Position.broker == "paper",
                                               Position.symbol == "US.AAPL"))
    assert pos.qty == 10

    titles = [e.title for e in env["dispatcher"].events]
    assert "订单成交" in titles


async def test_duplicate_alert_only_one_order(client, env, seeded):
    for _ in range(3):
        resp = await client.post(f"/webhook/tradingview/{_token()}", json=_payload())
        assert resp.status_code == 200
    await asyncio.sleep(0.2)
    signals = seeded.scalars(select(Signal)).all()
    orders = seeded.scalars(select(Order)).all()
    assert len(signals) == 1
    assert len(orders) == 1


async def test_bad_token_rejected(client):
    resp = await client.post("/webhook/tradingview/wrong-token", json=_payload())
    assert resp.status_code == 403


async def test_bad_secret_rejected(client):
    resp = await client.post(f"/webhook/tradingview/{_token()}", json=_payload(secret="wrong"))
    assert resp.status_code == 403


async def test_risk_block_flow(client, env, seeded):
    from app.db.models import RiskConfig

    cfg = seeded.get(RiskConfig, 1)
    cfg.trading_enabled = False  # kill switch
    seeded.commit()
    resp = await client.post(f"/webhook/tradingview/{_token()}",
                             json=_payload(alert_id="e2e-blocked"))
    assert resp.status_code == 200
    await asyncio.sleep(0.2)
    sig = seeded.scalar(select(Signal).where(Signal.dedup_key == "tv:e2e-blocked"))
    assert sig.status == "rejected_risk"
    assert seeded.scalars(select(Order)).all() == []


async def test_signal_only_mode_no_order(client, env, seeded):
    strat = seeded.scalar(select(StrategyConfig).where(StrategyConfig.name == "e2e"))
    strat.mode = "signal_only"
    seeded.commit()
    resp = await client.post(f"/webhook/tradingview/{_token()}",
                             json=_payload(alert_id="e2e-sig-only"))
    assert resp.status_code == 200
    await asyncio.sleep(0.2)
    assert seeded.scalars(select(Order)).all() == []
    sig = seeded.scalar(select(Signal).where(Signal.dedup_key == "tv:e2e-sig-only"))
    assert sig.status == "executed"


async def test_login_and_protected_api(client, seeded):
    resp = await client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200
    token = resp.json()["access_token"]

    resp = await client.get("/api/signals")
    assert resp.status_code == 401
    resp = await client.get("/api/signals", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
