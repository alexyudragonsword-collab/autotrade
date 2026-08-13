"""迭代10：期权内置策略（备兑 CoveredCall / 车轮 WheelStrategy）。"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.brokers.manager as manager_mod
import app.execution.order_manager as om_mod
import app.notify.dispatcher as dispatcher_mod
import app.risk.engine as risk_mod
import app.signals.pipeline as pipeline_mod
from app.brokers.paper import PaperBroker
from app.brokers.manager import BrokerManager
from app.db.models import Order, Position, RiskConfig, StrategyConfig
from app.domain.contracts import OptionContract
from app.domain.schemas import OptionChainItem
from app.execution.order_manager import OrderManager
from app.risk.engine import RiskEngine
from app.risk.rules import default_rules
from app.signals.pipeline import SignalPipeline

SPOT = 100.0


def _expiry(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y%m%d")


class FakeOptionBroker(PaperBroker):
    """paper 撮合 + 合成期权链（行权价 80~120 步长 5，权利金按虚值程度递减）。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.expirations = [_expiry(10), _expiry(30), _expiry(60)]

    async def get_option_expirations(self, underlying: str) -> list[str]:
        return list(self.expirations)

    async def get_option_chain(self, underlying: str, expiry: str,
                               with_quotes: bool = False, strikes_around: int | None = None):
        items = []
        for strike in range(80, 125, 5):
            for right in ("C", "P"):
                oc = OptionContract(underlying=underlying, expiry=expiry,
                                    right=right, strike=float(strike))
                # 简化定价：平值 5 元，每虚值 5 元便宜 1 元，下限 0.5
                otm = (strike - SPOT) if right == "C" else (SPOT - strike)
                premium = max(0.5, 5.0 - max(otm, 0) / 5)
                items.append(OptionChainItem(symbol=oc.symbol(), strike=float(strike),
                                             right=right, multiplier=100.0,
                                             bid=premium - 0.1, ask=premium + 0.1,
                                             last=premium))
        return items


@pytest.fixture
async def env(seeded, monkeypatch):
    async def quote(symbol):
        return SPOT  # 正股与期权撮合都用（期权信号带 price hint，quote 仅正股需要）

    broker = FakeOptionBroker(quote_fn=quote, fee_bps=0)
    await broker.connect()
    manager = BrokerManager()
    manager.register(broker)
    om = OrderManager(manager)
    om.attach_callbacks()

    class MockDispatcher:
        def __init__(self):
            self.events = []

        async def emit(self, event):
            self.events.append(event)

    monkeypatch.setattr(manager_mod, "_manager", manager)
    monkeypatch.setattr(om_mod, "_order_manager", om)
    monkeypatch.setattr(dispatcher_mod, "_dispatcher", MockDispatcher())
    monkeypatch.setattr(risk_mod, "_engine", RiskEngine(rules=default_rules(
        now_fn=lambda: datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc))))
    monkeypatch.setattr(pipeline_mod, "_pipeline", SignalPipeline())

    cfg = seeded.get(RiskConfig, 1)
    cfg.options_trading_enabled = True
    cfg.trading_hours_enabled = False
    cfg.max_order_value = 10_000_000
    cfg.max_position_value_per_symbol = 10_000_000
    cfg.max_total_exposure = 10_000_000
    seeded.commit()
    return {"broker": broker}


def _strategy_row(seeded, class_name: str, params: dict | None = None) -> int:
    seeded.add(StrategyConfig(name=f"cfg_{class_name}", class_name=class_name,
                              params=params or {}, enabled=True, mode="live",
                              broker="paper", symbols=["US.AAPL"]))
    seeded.commit()
    return seeded.scalar(select(StrategyConfig.id)
                         .where(StrategyConfig.name == f"cfg_{class_name}"))


# ---------- 注册表 ----------


def test_registry_lists_option_strategies(seeded):
    from app.strategy.registry import get_strategy_class, list_strategies

    items = {s["class_name"]: s for s in list_strategies()}
    assert items["CoveredCall"]["kind"] == "option"
    assert items["WheelStrategy"]["kind"] == "option"
    assert get_strategy_class("CoveredCall").params["otm_pct"] == 5.0


async def test_backtest_api_rejects_option_strategy(seeded):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        c.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
        resp = await c.post("/api/backtests", json={
            "strategy_class": "CoveredCall", "symbols": ["US.AAPL"], "market": "US",
            "start_date": "2025-01-01", "end_date": "2025-06-01"})
        assert resp.status_code == 400
        assert "不支持回测" in resp.json()["detail"]


# ---------- CoveredCall ----------


async def test_covered_call_sells_otm_call(env, seeded):
    from app.strategy.live import run_strategy_live

    # 持有 250 股正股 → 可备兑 2 张
    seeded.add(Position(broker="paper", symbol="US.AAPL", market="US",
                        qty=250, avg_cost=95.0))
    seeded.commit()
    cfg_id = _strategy_row(seeded, "CoveredCall",
                           {"otm_pct": 5, "min_dte": 20, "max_dte": 45})
    summary = await run_strategy_live(cfg_id)
    await asyncio.sleep(0.2)
    assert summary["errors"] == []
    assert summary["signals"] == 1

    orders = seeded.scalars(select(Order)).all()
    assert len(orders) == 1
    order = orders[0]
    assert order.side == "sell" and order.qty == 2 and order.status == "filled"
    oc = OptionContract.parse(order.symbol)
    assert oc.right == "C"
    assert oc.strike >= SPOT * 1.05  # 虚值 ≥5%
    assert oc.strike == 105.0  # 最近的满足档
    assert oc.expiry == _expiry(30)  # dte∈[20,45] 中最近的到期日
    seeded.expire_all()
    pos = seeded.scalar(select(Position).where(Position.symbol == order.symbol))
    assert pos.qty == -2

    # 再跑一次：已有有效空头 Call → 不重复开仓
    summary2 = await run_strategy_live(cfg_id)
    await asyncio.sleep(0.1)
    assert summary2["signals"] == 0


async def test_covered_call_insufficient_stock_skips(env, seeded):
    from app.strategy.live import run_strategy_live

    seeded.add(Position(broker="paper", symbol="US.AAPL", market="US",
                        qty=50, avg_cost=95.0))  # 不足 100 股
    seeded.commit()
    cfg_id = _strategy_row(seeded, "CoveredCall")
    summary = await run_strategy_live(cfg_id)
    assert summary["signals"] == 0
    assert summary["errors"] == []
    assert seeded.scalars(select(Order)).all() == []


async def test_covered_call_rolls_near_expiry(env, seeded):
    from app.strategy.live import run_strategy_live

    near = f"US.AAPL|{_expiry(2)}|C|110"
    seeded.add(Position(broker="paper", symbol="US.AAPL", market="US",
                        qty=100, avg_cost=95.0))
    seeded.add(Position(broker="paper", symbol=near, market="US",
                        qty=-1, avg_cost=1.0, multiplier=100))
    seeded.commit()
    cfg_id = _strategy_row(seeded, "CoveredCall", {"roll_dte": 3})
    summary = await run_strategy_live(cfg_id)
    await asyncio.sleep(0.2)
    # 只产生买回单（开新仓等下次运行，此时持仓快照已更新）
    assert summary["signals"] == 1
    order = seeded.scalars(select(Order)).all()[0]
    assert order.side == "buy" and order.symbol == near
    seeded.expire_all()
    pos = seeded.scalar(select(Position).where(Position.symbol == near))
    assert pos.qty == 0


# ---------- WheelStrategy ----------


async def test_wheel_sells_put_without_stock(env, seeded):
    from app.strategy.live import run_strategy_live

    cfg_id = _strategy_row(seeded, "WheelStrategy", {"otm_pct": 5, "put_contracts": 1})
    summary = await run_strategy_live(cfg_id)
    await asyncio.sleep(0.2)
    assert summary["errors"] == []
    assert summary["signals"] == 1
    order = seeded.scalars(select(Order)).all()[0]
    oc = OptionContract.parse(order.symbol)
    assert order.side == "sell" and oc.right == "P"
    assert oc.strike <= SPOT * 0.95
    assert oc.strike == 95.0


async def test_wheel_switches_to_call_after_assignment(env, seeded):
    from app.strategy.live import run_strategy_live

    # 模拟被行权接货：直接给 100 股正股，无期权持仓
    seeded.add(Position(broker="paper", symbol="US.AAPL", market="US",
                        qty=100, avg_cost=95.0))
    seeded.commit()
    cfg_id = _strategy_row(seeded, "WheelStrategy")
    summary = await run_strategy_live(cfg_id)
    await asyncio.sleep(0.2)
    assert summary["signals"] == 1
    order = seeded.scalars(select(Order)).all()[0]
    oc = OptionContract.parse(order.symbol)
    assert order.side == "sell" and oc.right == "C"  # 自动切换到备兑腿


async def test_wheel_insufficient_cash_skips(env, seeded, monkeypatch):
    from app.strategy.live import run_strategy_live

    # 把 paper 现金改小：担保 1 张 95×100=9500 不够
    from app.db.models import AppSetting

    row = seeded.get(AppSetting, "paper_cash")
    row.value = 5000
    seeded.commit()
    cfg_id = _strategy_row(seeded, "WheelStrategy")
    summary = await run_strategy_live(cfg_id)
    assert summary["signals"] == 0
    assert seeded.scalars(select(Order)).all() == []


# ---------- signal_only 模式 ----------


async def test_option_strategy_signal_only(env, seeded):
    from app.strategy.live import run_strategy_live

    seeded.add(Position(broker="paper", symbol="US.AAPL", market="US",
                        qty=100, avg_cost=95.0))
    seeded.add(StrategyConfig(name="cc_notify", class_name="CoveredCall",
                              enabled=True, mode="signal_only", broker="paper",
                              symbols=["US.AAPL"]))
    seeded.commit()
    cfg_id = seeded.scalar(select(StrategyConfig.id)
                           .where(StrategyConfig.name == "cc_notify"))
    summary = await run_strategy_live(cfg_id)
    await asyncio.sleep(0.2)
    assert summary["signals"] == 1  # 信号发出
    assert seeded.scalars(select(Order)).all() == []  # 但不下单
    events = dispatcher_mod.get_dispatcher().events
    assert any("仅提醒" in e.body for e in events)


# ---------- 编辑器支持自定义期权策略 ----------


def test_custom_option_strategy_compiles(seeded):
    from app.strategy.custom import compile_strategy_code, validate_strategy_class

    code = '''class MyOptionStrat(OptionStrategy):
    """测试自定义期权策略。"""

    params = {"otm_pct": 10}

    async def on_run(self, ctx):
        item = await ctx.select_contract("C", 20, 45, self.p["otm_pct"])
        if item is not None and ctx.stock_qty() >= item.multiplier:
            ctx.sell_open(item.symbol, 1, price=self.mid_or_last(item),
                          multiplier=item.multiplier)
'''
    cls = compile_strategy_code(code)
    report = validate_strategy_class(cls)
    assert report["kind"] == "option"


# ---------- CashSecuredPut ----------


async def test_csp_sells_put(env, seeded):
    from app.strategy.live import run_strategy_live

    cfg_id = _strategy_row(seeded, "CashSecuredPut",
                           {"otm_pct": 5, "contracts": 2})
    summary = await run_strategy_live(cfg_id)
    await asyncio.sleep(0.2)
    assert summary["errors"] == []
    assert summary["signals"] == 1
    order = seeded.scalars(select(Order)).all()[0]
    oc = OptionContract.parse(order.symbol)
    assert order.side == "sell" and order.qty == 2
    assert oc.right == "P" and oc.strike == 95.0

    # 已有有效空头 Put → 不加仓
    summary2 = await run_strategy_live(cfg_id)
    await asyncio.sleep(0.1)
    assert summary2["signals"] == 0


async def test_csp_ignores_stock_and_never_sells_call(env, seeded):
    """与车轮的区别：持有正股也继续只卖 Put，绝不卖 Call。"""
    from app.strategy.live import run_strategy_live

    seeded.add(Position(broker="paper", symbol="US.AAPL", market="US",
                        qty=200, avg_cost=95.0))
    seeded.commit()
    cfg_id = _strategy_row(seeded, "CashSecuredPut")
    summary = await run_strategy_live(cfg_id)
    await asyncio.sleep(0.2)
    assert summary["signals"] == 1
    order = seeded.scalars(select(Order)).all()[0]
    assert OptionContract.parse(order.symbol).right == "P"


async def test_csp_rolls_near_expiry(env, seeded):
    from app.strategy.live import run_strategy_live

    near = f"US.AAPL|{_expiry(2)}|P|95"
    seeded.add(Position(broker="paper", symbol=near, market="US",
                        qty=-1, avg_cost=2.0, multiplier=100))
    seeded.commit()
    cfg_id = _strategy_row(seeded, "CashSecuredPut", {"roll_dte": 3})
    summary = await run_strategy_live(cfg_id)
    await asyncio.sleep(0.2)
    assert summary["signals"] == 1  # 只买回，开新仓等下轮
    order = seeded.scalars(select(Order)).all()[0]
    assert order.side == "buy" and order.symbol == near


async def test_csp_insufficient_cash_skips(env, seeded):
    from app.db.models import AppSetting
    from app.strategy.live import run_strategy_live

    row = seeded.get(AppSetting, "paper_cash")
    row.value = 5000  # 担保 1 张需 9500
    seeded.commit()
    cfg_id = _strategy_row(seeded, "CashSecuredPut")
    summary = await run_strategy_live(cfg_id)
    assert summary["signals"] == 0
    assert seeded.scalars(select(Order)).all() == []
