"""迭代5：多账户与持仓守护。"""

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

import app.brokers.manager as manager_mod
import app.execution.order_manager as om_mod
import app.notify.dispatcher as dispatcher_mod
import app.risk.engine as risk_mod
from app.brokers.manager import BrokerManager
from app.brokers.paper import PaperBroker
from app.db.models import Order, Position, RiskConfig, Signal
from app.execution.order_manager import OrderManager
from app.risk.engine import RiskEngine
from app.risk.guard import _check_triggers, run_position_guard
from app.risk.rules import default_rules


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
    om = OrderManager(manager)
    om.attach_callbacks()
    dispatcher = MockDispatcher()

    monkeypatch.setattr(manager_mod, "_manager", manager)
    monkeypatch.setattr(om_mod, "_order_manager", om)
    monkeypatch.setattr(dispatcher_mod, "_dispatcher", dispatcher)
    monkeypatch.setattr(risk_mod, "_engine", RiskEngine(rules=default_rules(
        now_fn=lambda: datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc))))
    return {"price": price_holder, "manager": manager, "dispatcher": dispatcher, "om": om}


# ---------- 多账户 ----------


async def test_multiple_paper_accounts_isolated_cash(seeded):
    async def quote(symbol):
        return 10.0

    a = PaperBroker(name="paper", quote_fn=quote, fee_bps=0)
    b = PaperBroker(name="paper2", quote_fn=quote, fee_bps=0, initial_cash=5000)
    await a.connect()
    await b.connect()
    acc_a = await a.get_account()
    acc_b = await b.get_account()
    assert acc_a.cash == 1_000_000  # env 默认
    assert acc_b.cash == 5000

    from app.domain.enums import Market, OrderSide, OrderType
    from app.domain.schemas import OrderRequest

    await b.place_order(OrderRequest("US.X", Market.US, OrderSide.BUY, OrderType.MARKET, 100))
    await asyncio.sleep(0.05)
    # b 扣钱、持仓归 b；a 不受影响
    assert (await b.get_account()).cash == pytest.approx(4000)
    assert (await a.get_account()).cash == 1_000_000
    pos = seeded.scalars(select(Position).where(Position.qty != 0)).all()
    assert len(pos) == 1
    assert pos[0].broker == "paper2"


async def test_manager_add_remove_account(env, seeded):
    manager = env["manager"]
    adapter = await manager.add_account("paper", "paper_extra", {"initial_cash": 8888})
    env["om"].attach_adapter(adapter)
    assert manager.get("paper_extra") is adapter
    assert (await adapter.get_account()).cash == 8888
    with pytest.raises(Exception):
        await manager.add_account("paper", "paper_extra", {})  # 重名
    await manager.remove_account("paper_extra")
    assert manager.get_if_connected("paper_extra") is None


async def test_broker_account_api(env, seeded):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        c.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"

        resp = await c.post("/api/broker-accounts",
                            json={"name": "sim2", "type": "paper",
                                  "params": {"initial_cash": 66666}})
        assert resp.status_code == 200
        rows = (await c.get("/api/broker-accounts")).json()
        sim2 = next(r for r in rows if r["name"] == "sim2")
        assert sim2["connected"] is True

        # 类型/重名校验
        assert (await c.post("/api/broker-accounts",
                             json={"name": "x", "type": "bad"})).status_code == 400
        assert (await c.post("/api/broker-accounts",
                             json={"name": "sim2", "type": "paper"})).status_code == 400

        # 有持仓不能删
        seeded.add(Position(broker="sim2", symbol="US.HOLD", market="US", qty=5, avg_cost=10))
        seeded.commit()
        resp = await c.delete(f"/api/broker-accounts/{sim2['id']}")
        assert resp.status_code == 400
        seeded.query(Position).delete()
        seeded.commit()
        assert (await c.delete(f"/api/broker-accounts/{sim2['id']}")).status_code == 200


# ---------- 持仓守护 ----------


def _pos(**overrides) -> Position:
    base = dict(broker="paper", symbol="US.G", market="US", qty=10, avg_cost=100.0,
                high_water_price=None)
    base.update(overrides)
    return Position(**base)


def test_check_triggers(seeded):
    cfg = seeded.get(RiskConfig, 1)
    cfg.stop_loss_pct = 5.0
    cfg.take_profit_pct = 20.0
    cfg.trailing_stop_pct = 8.0

    pos = _pos(high_water_price=130.0)
    assert _check_triggers(cfg, pos, 94.9) is not None  # 亏 5.1% → 止损
    assert "止损" in _check_triggers(cfg, pos, 94.9)
    assert "止盈" in _check_triggers(cfg, pos, 121.0)  # 赚 21%
    assert "移动止损" in _check_triggers(cfg, pos, 119.0)  # 距 130 回撤 8.5%
    assert "止盈" in _check_triggers(cfg, pos, 120.0)  # 恰好 20% 边界触发
    # 无任何触发（高水位内、亏损内、止盈下方）
    pos2 = _pos(high_water_price=105.0)
    assert _check_triggers(cfg, pos2, 101.0) is None


async def test_guard_stop_loss_closes_position(env, seeded):
    cfg = seeded.get(RiskConfig, 1)
    cfg.stop_loss_pct = 5.0
    seeded.commit()
    seeded.add(_pos(avg_cost=100.0))
    seeded.commit()

    env["price"]["p"] = 90.0  # 亏 10%
    triggered = await run_position_guard()
    await asyncio.sleep(0.15)
    assert len(triggered) == 1
    assert "止损" in triggered[0]["reason"]

    sig = seeded.scalar(select(Signal).where(Signal.source == "risk_guard"))
    assert sig is not None
    order = seeded.scalar(select(Order).where(Order.signal_id == sig.id))
    assert order.side == "sell"
    assert order.status == "filled"
    seeded.expire_all()
    pos = seeded.scalar(select(Position).where(Position.symbol == "US.G"))
    assert pos.qty == 0
    assert any(e.title == "持仓守护平仓" for e in env["dispatcher"].events)


async def test_guard_updates_high_water_and_trailing(env, seeded):
    cfg = seeded.get(RiskConfig, 1)
    cfg.trailing_stop_pct = 10.0
    seeded.commit()
    seeded.add(_pos(avg_cost=100.0))
    seeded.commit()

    env["price"]["p"] = 150.0  # 抬高水位
    assert await run_position_guard() == []
    seeded.expire_all()
    pos = seeded.scalar(select(Position).where(Position.symbol == "US.G"))
    assert pos.high_water_price == 150.0

    env["price"]["p"] = 134.0  # 距 150 回撤 10.7% → 触发
    triggered = await run_position_guard()
    await asyncio.sleep(0.15)
    assert len(triggered) == 1
    assert "移动止损" in triggered[0]["reason"]


async def test_guard_respects_kill_switch(env, seeded):
    cfg = seeded.get(RiskConfig, 1)
    cfg.stop_loss_pct = 5.0
    cfg.trading_enabled = False
    seeded.commit()
    seeded.add(_pos(avg_cost=100.0))
    seeded.commit()
    env["price"]["p"] = 50.0
    triggered = await run_position_guard()
    assert triggered == []
    assert seeded.scalars(select(Order)).all() == []


async def test_guard_no_duplicate_while_order_open(env, seeded):
    cfg = seeded.get(RiskConfig, 1)
    cfg.stop_loss_pct = 5.0
    seeded.commit()
    seeded.add(_pos(avg_cost=100.0))
    seeded.add(Order(broker="paper", symbol="US.G", market="US", side="sell",
                     order_type="market", qty=10, status="submitted"))
    seeded.commit()
    env["price"]["p"] = 90.0
    triggered = await run_position_guard()
    assert triggered == []  # 已有在途卖单，不重复触发
