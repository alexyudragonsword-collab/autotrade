"""迭代7：绩效归因与通知路由。"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models import NotifyChannel, Order, Signal, TradeFill
from app.domain.enums import NotifyLevel
from app.domain.schemas import NotifyEvent
from app.notify.dispatcher import channel_matches


def _seed_fills(db):
    """两个策略、两个账户的成交数据。"""
    now = datetime.now(timezone.utc)
    sig1 = Signal(source="tradingview", dedup_key="p1", strategy_name="alpha",
                  symbol="US.A", market="US", action="sell", status="executed")
    sig2 = Signal(source="tradingview", dedup_key="p2", strategy_name="beta",
                  symbol="US.B", market="US", action="sell", status="executed")
    db.add_all([sig1, sig2])
    db.flush()
    o1 = Order(signal_id=sig1.id, broker="paper", symbol="US.A", market="US", side="sell",
               order_type="market", qty=10, status="filled")
    o2 = Order(signal_id=sig2.id, broker="live1", symbol="US.B", market="US", side="sell",
               order_type="market", qty=5, status="filled")
    o3 = Order(signal_id=None, broker="paper", symbol="US.C", market="US", side="sell",
               order_type="market", qty=3, status="filled")  # 手动单
    db.add_all([o1, o2, o3])
    db.flush()
    db.add_all([
        TradeFill(order_id=o1.id, qty=10, price=110, fee=1.0, realized_pnl=100.0, ts=now),
        TradeFill(order_id=o2.id, qty=5, price=90, fee=0.5, realized_pnl=-50.0, ts=now),
        TradeFill(order_id=o3.id, qty=3, price=100, fee=0.3, realized_pnl=30.0,
                  ts=now - timedelta(days=1)),
    ])
    db.commit()


@pytest.fixture
async def client(seeded):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        c.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
        yield c


async def test_performance_summary(client, seeded):
    _seed_fills(seeded)
    resp = await client.get("/api/performance/summary", params={"days": 7})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_realized_pnl"] == pytest.approx(80.0)  # 100 - 50 + 30
    assert data["total_fees"] == pytest.approx(1.8)

    by_strategy = {r["key"]: r for r in data["by_strategy"]}
    assert by_strategy["alpha"]["realized_pnl"] == 100.0
    assert by_strategy["alpha"]["win_rate"] == 1.0
    assert by_strategy["beta"]["realized_pnl"] == -50.0
    assert by_strategy["beta"]["win_rate"] == 0.0
    assert "unknown" in by_strategy  # 无信号的手动单归入 unknown

    by_account = {r["key"]: r for r in data["by_account"]}
    assert by_account["paper"]["realized_pnl"] == pytest.approx(130.0)
    assert by_account["live1"]["realized_pnl"] == -50.0

    assert len(data["daily_pnl"]) == 2  # 今天 + 昨天


async def test_performance_days_filter(client, seeded):
    _seed_fills(seeded)
    # days=0 → 只统计今天（昨天的 30 被排除）
    resp = await client.get("/api/performance/summary", params={"days": 0})
    assert resp.json()["total_realized_pnl"] == pytest.approx(50.0)


async def test_snapshot_upsert(client, seeded, monkeypatch):
    import app.brokers.manager as manager_mod
    from app.brokers.manager import BrokerManager
    from app.brokers.paper import PaperBroker
    from app.db.models import AccountValueSnapshot

    async def quote(symbol):
        return 100.0

    broker = PaperBroker(quote_fn=quote)
    await broker.connect()
    manager = BrokerManager()
    manager.register(broker)
    monkeypatch.setattr(manager_mod, "_manager", manager)

    resp = await client.post("/api/performance/snapshot")
    assert resp.json()["recorded"] == 1
    resp = await client.post("/api/performance/snapshot")  # 同日再记 → upsert 不重复
    assert resp.json()["recorded"] == 1
    rows = seeded.scalars(select(AccountValueSnapshot)).all()
    assert len(rows) == 1
    assert rows[0].net_value == pytest.approx(1_000_000)

    resp = await client.get("/api/performance/equity")
    curves = resp.json()
    assert "paper" in curves and len(curves["paper"]) == 1


# ---------- 通知路由 ----------


def _channel(**overrides) -> NotifyChannel:
    base = dict(type="telegram", name="t", enabled=True, min_level="info", config={})
    base.update(overrides)
    return NotifyChannel(**base)


def _event(**overrides) -> NotifyEvent:
    base = dict(level=NotifyLevel.INFO, title="t", body="b")
    base.update(overrides)
    return NotifyEvent(**base)


def test_channel_matches_level():
    ch = _channel(min_level="warn")
    assert not channel_matches(ch, _event(level=NotifyLevel.INFO))
    assert channel_matches(ch, _event(level=NotifyLevel.WARN))
    assert channel_matches(ch, _event(level=NotifyLevel.ERROR))


def test_channel_matches_strategy_filter():
    ch = _channel(config={"strategies": ["alpha"]})
    assert channel_matches(ch, _event(strategy="alpha"))
    assert not channel_matches(ch, _event(strategy="beta"))
    # 系统级事件（无策略元数据）始终投递
    assert channel_matches(ch, _event(strategy=None))


def test_channel_matches_broker_filter():
    ch = _channel(config={"brokers": ["live1"]})
    assert channel_matches(ch, _event(broker="live1"))
    assert not channel_matches(ch, _event(broker="paper"))
    assert channel_matches(ch, _event(broker=None))


def test_channel_matches_combined():
    ch = _channel(config={"strategies": ["alpha"], "brokers": ["live1"]})
    assert channel_matches(ch, _event(strategy="alpha", broker="live1"))
    assert not channel_matches(ch, _event(strategy="alpha", broker="paper"))
    assert not channel_matches(ch, _event(strategy="beta", broker="live1"))
    # 空过滤 = 不限
    assert channel_matches(_channel(config={"strategies": [], "brokers": []}),
                           _event(strategy="anything", broker="anywhere"))


async def test_dispatcher_routes_by_filter(seeded, monkeypatch):
    """两个渠道不同过滤条件，事件只投递到匹配的渠道。"""
    import app.notify.dispatcher as dispatcher_mod
    from app.notify.dispatcher import NotifyDispatcher

    seeded.add(_channel(type="telegram", config={"strategies": ["alpha"]}))
    seeded.add(_channel(type="wecom", config={"strategies": ["beta"]}))
    seeded.commit()

    sent = []

    class FakeNotifier:
        def __init__(self, t):
            self.type = t

        async def send(self, event):
            sent.append(self.type)

    monkeypatch.setattr(dispatcher_mod, "_NOTIFIERS",
                        {"telegram": FakeNotifier("telegram"), "wecom": FakeNotifier("wecom")})
    dispatcher = NotifyDispatcher()
    await dispatcher.emit(_event(strategy="alpha", broker="paper"))
    assert sent == ["telegram"]
    sent.clear()
    await dispatcher.emit(_event(strategy=None))  # 系统级 → 全部
    assert sorted(sent) == ["telegram", "wecom"]
