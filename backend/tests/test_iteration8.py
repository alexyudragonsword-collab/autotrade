"""迭代8：WebSocket 实时推送与回测报告导出。"""

import asyncio

import pytest
from starlette.testclient import TestClient

from app.api.deps import create_access_token
from app.db.models import BacktestRun
from app.events import EventBus, get_event_bus


# ---------- 事件总线 ----------


async def test_event_bus_pub_sub():
    bus = EventBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    bus.publish("signal", {"id": 1})
    msg1 = q1.get_nowait()
    msg2 = q2.get_nowait()
    assert msg1["type"] == "signal" and msg1["data"]["id"] == 1
    assert msg2["data"] == msg1["data"]
    bus.unsubscribe(q2)
    bus.publish("order_update", {"id": 2})
    assert q1.get_nowait()["type"] == "order_update"
    assert q2.empty()


async def test_event_bus_slow_consumer_drops_oldest():
    bus = EventBus()
    q = bus.subscribe()
    for i in range(250):  # 超过队列上限 200
        bus.publish("t", {"i": i})
    # 最新消息保留（最旧被丢弃），队列不阻塞
    items = []
    while not q.empty():
        items.append(q.get_nowait()["data"]["i"])
    assert len(items) == 200
    assert items[-1] == 249


# ---------- WebSocket ----------


def test_ws_rejects_bad_token(seeded):
    from app.main import create_app

    client = TestClient(create_app())
    with pytest.raises(Exception):
        with client.websocket_connect("/ws?token=invalid"):
            pass


def test_ws_receives_events(seeded):
    from app.main import create_app

    client = TestClient(create_app())
    token = create_access_token("admin")
    with client.websocket_connect(f"/ws?token={token}") as ws:
        get_event_bus().publish("signal", {"id": 7, "symbol": "US.WS"})
        msg = ws.receive_json()
        assert msg["type"] == "signal"
        assert msg["data"]["symbol"] == "US.WS"


async def test_pipeline_publishes_events(seeded, monkeypatch):
    """信号处理与订单回报会广播到事件总线。"""
    from datetime import datetime, timezone

    import app.brokers.manager as manager_mod
    import app.events as events_mod
    import app.execution.order_manager as om_mod
    import app.notify.dispatcher as dispatcher_mod
    import app.risk.engine as risk_mod
    import app.signals.pipeline as pipeline_mod
    from app.brokers.manager import BrokerManager
    from app.brokers.paper import PaperBroker
    from app.db.models import StrategyConfig
    from app.domain.enums import Market, OrderType, SignalAction
    from app.domain.schemas import NormalizedSignal
    from app.execution.order_manager import OrderManager
    from app.risk.engine import RiskEngine
    from app.risk.rules import default_rules
    from app.signals.pipeline import SignalPipeline

    async def quote(symbol):
        return 100.0

    broker = PaperBroker(quote_fn=quote, fee_bps=0)
    await broker.connect()
    manager = BrokerManager()
    manager.register(broker)
    om = OrderManager(manager)
    om.attach_callbacks()

    class MockDispatcher:
        async def emit(self, event):
            pass

    fresh_bus = EventBus()
    monkeypatch.setattr(events_mod, "_bus", fresh_bus)
    monkeypatch.setattr(manager_mod, "_manager", manager)
    monkeypatch.setattr(om_mod, "_order_manager", om)
    monkeypatch.setattr(dispatcher_mod, "_dispatcher", MockDispatcher())
    monkeypatch.setattr(risk_mod, "_engine", RiskEngine(rules=default_rules(
        now_fn=lambda: datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc))))
    monkeypatch.setattr(pipeline_mod, "_pipeline", SignalPipeline())

    seeded.add(StrategyConfig(name="ws_test", enabled=True, mode="live",
                              broker="paper", default_qty=5))
    seeded.commit()

    queue = fresh_bus.subscribe()
    await pipeline_mod.get_pipeline().process(NormalizedSignal(
        dedup_key="ws:1", strategy="ws_test", symbol="US.WS", market=Market.US,
        action=SignalAction.BUY, quantity=5, order_type=OrderType.MARKET, price=100.0))
    await asyncio.sleep(0.15)

    types = []
    while not queue.empty():
        types.append(queue.get_nowait()["type"])
    assert "signal" in types
    assert "order_update" in types


# ---------- 回测报告 ----------


@pytest.fixture
async def client(seeded):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        c.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
        yield c


def _done_run() -> BacktestRun:
    return BacktestRun(
        strategy_class="SmaCross", params={"fast": 5}, symbols=["US.RPT"], market="US",
        start_date="2026-01-01", end_date="2026-03-01", status="done",
        metrics={"total_return": 0.12, "annual_return": 0.3, "sharpe": 1.5,
                 "max_drawdown": -0.08, "win_rate": 0.6, "trade_count": 4,
                 "final_equity": 112000.0, "benchmark_return": 0.05, "alpha": 0.07,
                 "monthly_returns": [{"month": "2026-01", "ret": 0.05},
                                     {"month": "2026-02", "ret": 0.066}]},
        equity_curve=[["2026-01-02", 100000, 100000], ["2026-02-27", 112000, 105000]],
        trades=[{"date": "2026-01-05", "symbol": "US.RPT", "side": "buy",
                 "qty": 10, "price": 100.0, "fee": 0.3, "pnl": None},
                {"date": "2026-02-10", "symbol": "US.RPT", "side": "sell",
                 "qty": 10, "price": 112.0, "fee": 0.3, "pnl": 119.4}])


async def test_report_download(client, seeded):
    run = _done_run()
    seeded.add(run)
    seeded.commit()
    resp = await client.get(f"/api/backtests/{run.id}/report")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    body = resp.text
    assert "SmaCross" in body
    assert "12.00%" in body  # 总收益
    assert "买入持有基准" in body
    assert "<polyline" in body  # 内联 SVG 曲线
    assert "2026-01" in body  # 月度收益
    assert "买入" in body and "卖出" in body


async def test_report_requires_done(client, seeded):
    run = _done_run()
    run.status = "running"
    seeded.add(run)
    seeded.commit()
    resp = await client.get(f"/api/backtests/{run.id}/report")
    assert resp.status_code == 400
