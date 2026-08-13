"""迭代9：期权交易支持（合约模型/风控/空头/守护/到期/入口）。"""

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

import app.brokers.manager as manager_mod
import app.execution.order_manager as om_mod
import app.notify.dispatcher as dispatcher_mod
import app.risk.engine as risk_mod
import app.signals.pipeline as pipeline_mod
from app.brokers.manager import BrokerManager
from app.brokers.paper import PaperBroker
from app.db.models import Order, Position, RiskConfig, Signal, TradeFill
from app.domain.contracts import (
    OptionContract,
    days_to_expiry,
    default_multiplier,
    is_option,
    parse_futu_us_option_code,
    to_futu_us_option_code,
    underlying_of,
)
from app.domain.enums import Market, OrderSide, OrderType
from app.domain.schemas import OrderIntent
from app.execution.order_manager import OrderManager
from app.risk.engine import RiskEngine
from app.risk.rules import default_rules
from app.signals.pipeline import SignalPipeline

CALL = "US.AAPL|20991218|C|230"  # 远期到期，避免测试跨时间失效
PUT = "US.AAPL|20991218|P|200"


# ---------- 合约模型 ----------


def test_contract_roundtrip():
    oc = OptionContract.parse(CALL)
    assert oc.underlying == "US.AAPL" and oc.right == "C" and oc.strike == 230.0
    assert oc.symbol() == CALL
    assert oc.market == Market.US
    # 小数行权价
    frac = OptionContract("US.SPY", "20991218", "P", 432.5)
    assert OptionContract.parse(frac.symbol()).strike == 432.5
    # 非期权
    assert OptionContract.parse("US.AAPL") is None
    assert not is_option("HK.00700")
    assert is_option(PUT)
    assert underlying_of(CALL) == "US.AAPL"
    assert underlying_of("US.TSLA") == "US.TSLA"
    # 非法格式
    assert OptionContract.parse("US.AAPL|2099|C|230") is None
    assert OptionContract.parse("US.AAPL|20991218|X|230") is None


def test_days_to_expiry_and_multiplier():
    assert days_to_expiry(CALL) > 20000  # 2099 年
    assert days_to_expiry("US.AAPL") is None
    assert default_multiplier(CALL) == 100.0
    assert default_multiplier("US.AAPL") == 1.0
    assert default_multiplier("HK.00700|20991218|C|360") == 100.0  # settings 默认


def test_futu_us_code_roundtrip():
    oc = OptionContract("US.AAPL", "20250919", "C", 230.0)
    code = to_futu_us_option_code(oc)
    assert code == "US.AAPL250919C230000"
    back = parse_futu_us_option_code(code)
    assert back == oc
    # 小数行权价
    oc2 = OptionContract("US.SPY", "20251219", "P", 432.5)
    assert parse_futu_us_option_code(to_futu_us_option_code(oc2)) == oc2
    assert parse_futu_us_option_code("US.AAPL") is None


# ---------- TV 告警解析 ----------


def test_parser_option_fields():
    from app.signals.parser import SignalParseError, parse_tv_alert

    base = {"secret": "x", "alert_id": "o1", "strategy": "s", "symbol": "AAPL",
            "exchange": "NASDAQ", "action": "buy", "qty": 1,
            "expiry": "2099-12-18", "strike": 230, "right": "call"}
    sig = parse_tv_alert(base)
    assert sig.symbol == CALL
    assert sig.market == Market.US

    # 缺字段
    partial = dict(base)
    del partial["strike"]
    with pytest.raises(SignalParseError, match="expiry / strike / right"):
        parse_tv_alert(partial)
    # 非法 right
    with pytest.raises(SignalParseError, match="right"):
        parse_tv_alert({**base, "right": "X"})
    # CN 拒绝
    with pytest.raises(SignalParseError, match="A股"):
        parse_tv_alert({**base, "symbol": "600519", "exchange": "SSE"})


# ---------- 测试环境 ----------


class MockDispatcher:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


@pytest.fixture
async def env(seeded, monkeypatch):
    price_holder = {"p": 5.0}  # 期权权利金

    async def quote(symbol):
        return price_holder["p"]

    broker = PaperBroker(quote_fn=quote, fee_bps=0)
    await broker.connect()
    manager = BrokerManager()
    manager.register(broker)
    om = OrderManager(manager)
    om.attach_callbacks()
    dispatcher = MockDispatcher()
    engine = RiskEngine(rules=default_rules(
        now_fn=lambda: datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)))

    monkeypatch.setattr(manager_mod, "_manager", manager)
    monkeypatch.setattr(om_mod, "_order_manager", om)
    monkeypatch.setattr(dispatcher_mod, "_dispatcher", dispatcher)
    monkeypatch.setattr(risk_mod, "_engine", engine)
    monkeypatch.setattr(pipeline_mod, "_pipeline", SignalPipeline())

    cfg = seeded.get(RiskConfig, 1)
    cfg.options_trading_enabled = True
    cfg.trading_hours_enabled = False
    cfg.max_order_value = 10_000_000
    cfg.max_position_value_per_symbol = 10_000_000
    cfg.max_total_exposure = 10_000_000
    seeded.commit()
    return {"price": price_holder, "engine": engine, "dispatcher": dispatcher, "om": om}


def _intent(**overrides) -> OrderIntent:
    base = dict(symbol=CALL, market=Market.US, side=OrderSide.SELL,
                order_type=OrderType.MARKET, qty=1, est_price=5.0,
                broker="paper", multiplier=100.0)
    base.update(overrides)
    return OrderIntent(**base)


# ---------- 风控 ----------


async def test_options_disabled_blocks(env, seeded):
    cfg = seeded.get(RiskConfig, 1)
    cfg.options_trading_enabled = False
    seeded.commit()
    decision = env["engine"].check(seeded, _intent(side=OrderSide.BUY))
    assert not decision.allowed
    assert decision.rule_name == "options_enabled"
    # 股票不受影响
    assert env["engine"].check(seeded, OrderIntent(
        symbol="US.AAPL", market=Market.US, side=OrderSide.BUY,
        order_type=OrderType.MARKET, qty=1, est_price=100.0, broker="paper")).allowed


async def test_covered_call(env, seeded):
    # 无正股 → 拒
    decision = env["engine"].check(seeded, _intent(), account_cash=1_000_000)
    assert not decision.allowed
    assert decision.rule_name == "covered_or_secured"
    # 持有 100 股正股 → 备兑 1 张放行
    seeded.add(Position(broker="paper", symbol="US.AAPL", market="US", qty=100, avg_cost=230))
    seeded.commit()
    assert env["engine"].check(seeded, _intent(), account_cash=0).allowed
    # 2 张需要 200 股 → 拒
    assert not env["engine"].check(seeded, _intent(qty=2), account_cash=0).allowed
    # 已有 1 张空 Call 占用 100 股备兑 → 再卖 1 张拒
    seeded.add(Position(broker="paper", symbol="US.AAPL|20991218|C|240", market="US",
                        qty=-1, avg_cost=3.0, multiplier=100))
    seeded.commit()
    assert not env["engine"].check(seeded, _intent(), account_cash=0).allowed


async def test_cash_secured_put(env, seeded):
    # 现金足额（200×100=20000）→ 放行
    assert env["engine"].check(seeded, _intent(symbol=PUT), account_cash=25_000).allowed
    # 不足 → 拒
    decision = env["engine"].check(seeded, _intent(symbol=PUT), account_cash=15_000)
    assert not decision.allowed and "现金担保不足" in decision.reason
    # 现金未知且无快照 → fail-closed
    decision = env["engine"].check(seeded, _intent(symbol=PUT), account_cash=None)
    assert not decision.allowed and "无法获取" in decision.reason
    # 已有空 Put 占用担保：再卖需 20000+20000 > 30000 → 拒
    seeded.add(Position(broker="paper", symbol="US.AAPL|20991218|P|200", market="US",
                        qty=-1, avg_cost=4.0, multiplier=100))
    seeded.commit()
    decision = env["engine"].check(seeded, _intent(symbol="US.AAPL|20991218|P|200", qty=1),
                                   account_cash=30_000)
    assert not decision.allowed


async def test_naked_selling_switch(env, seeded):
    cfg = seeded.get(RiskConfig, 1)
    cfg.allow_naked_selling = True
    cfg.max_short_option_notional = 30_000
    seeded.commit()
    # 裸卖 1 张 Call（名义 230×100=23000 < 30000）→ 放行（无需正股/现金）
    assert env["engine"].check(seeded, _intent(), account_cash=None).allowed
    # 2 张（46000 > 30000）→ 拒
    decision = env["engine"].check(seeded, _intent(qty=2), account_cash=None)
    assert not decision.allowed
    assert decision.rule_name == "naked_notional"


async def test_buy_to_close_bypasses_caps(env, seeded):
    cfg = seeded.get(RiskConfig, 1)
    cfg.max_total_exposure = 1.0  # 极小限额
    cfg.max_position_value_per_symbol = 1.0
    seeded.commit()
    seeded.add(Position(broker="paper", symbol=CALL, market="US", qty=-2,
                        avg_cost=5.0, multiplier=100))
    seeded.commit()
    # 买回平空 2 张：不受限额约束
    assert env["engine"].check(seeded, _intent(side=OrderSide.BUY, qty=2)).allowed
    # 买 3 张（超出空头）→ 受限额约束被拒
    assert not env["engine"].check(seeded, _intent(side=OrderSide.BUY, qty=3)).allowed


async def test_whitelist_matches_underlying(env, seeded):
    cfg = seeded.get(RiskConfig, 1)
    cfg.symbol_whitelist = ["US.AAPL"]
    cfg.allow_naked_selling = True
    seeded.commit()
    assert env["engine"].check(seeded, _intent(side=OrderSide.BUY)).allowed
    decision = env["engine"].check(seeded, _intent(
        symbol="US.TSLA|20991218|C|300", side=OrderSide.BUY))
    assert not decision.allowed
    assert decision.rule_name == "symbol_whitelist"


# ---------- Paper 空头与盈亏 ----------


async def test_paper_sell_to_open_and_buy_to_close(env, seeded):
    cfg = seeded.get(RiskConfig, 1)
    cfg.allow_naked_selling = True
    seeded.commit()
    om = env["om"]

    # 卖开 2 张 @5，乘数 100 → 收权利金 1000
    await om.submit(_intent(qty=2))
    await asyncio.sleep(0.1)
    pos = seeded.scalar(select(Position).where(Position.symbol == CALL))
    assert pos.qty == -2
    assert pos.avg_cost == 5.0
    assert pos.multiplier == 100.0
    broker = manager_mod.get_broker_manager().get("paper")
    account = await broker.get_account()
    assert account.cash == pytest.approx(1_000_000 + 1000)

    # 权利金跌到 3 → 买回平仓，盈利 (5-3)*2*100 = 400
    env["price"]["p"] = 3.0
    await om.submit(_intent(side=OrderSide.BUY, qty=2, est_price=3.0))
    await asyncio.sleep(0.1)
    seeded.expire_all()
    pos = seeded.scalar(select(Position).where(Position.symbol == CALL))
    assert pos.qty == 0
    fills = seeded.scalars(select(TradeFill).order_by(TradeFill.id)).all()
    assert fills[0].realized_pnl is None  # 卖开不计
    assert fills[1].realized_pnl == pytest.approx(400.0)
    account = await broker.get_account()
    assert account.cash == pytest.approx(1_000_000 + 1000 - 600)


async def test_paper_stock_still_no_short(env, seeded):
    om = env["om"]
    await om.submit(OrderIntent(symbol="US.AAPL", market=Market.US, side=OrderSide.SELL,
                                order_type=OrderType.MARKET, qty=10, est_price=100.0,
                                broker="paper"))
    await asyncio.sleep(0.1)
    pos = seeded.scalar(select(Position).where(Position.symbol == "US.AAPL"))
    assert pos is None or pos.qty == 0  # 无持仓卖出被截断为 0，不产生空头


async def test_long_option_full_close_realized_pnl(env, seeded):
    """买方全平也要有盈亏（修复：全平后均价清零导致 realized 丢失）。"""
    om = env["om"]
    await om.submit(_intent(side=OrderSide.BUY, qty=1))  # 买 1 张 @5
    await asyncio.sleep(0.1)
    env["price"]["p"] = 8.0
    await om.submit(_intent(side=OrderSide.SELL, qty=1, est_price=8.0))  # 全平 @8
    await asyncio.sleep(0.1)
    fills = seeded.scalars(select(TradeFill).order_by(TradeFill.id)).all()
    assert fills[1].realized_pnl == pytest.approx((8 - 5) * 1 * 100)


# ---------- 守护空头 + 到期 ----------


async def test_guard_short_stop_loss(env, seeded):
    from app.risk.guard import run_position_guard

    cfg = seeded.get(RiskConfig, 1)
    cfg.stop_loss_pct = 20.0
    seeded.commit()
    seeded.add(Position(broker="paper", symbol=CALL, market="US", qty=-2,
                        avg_cost=5.0, multiplier=100))
    seeded.commit()

    env["price"]["p"] = 5.5  # 涨 10%，未触发
    assert await run_position_guard() == []
    env["price"]["p"] = 6.5  # 涨 30% → 空头止损
    triggered = await run_position_guard()
    await asyncio.sleep(0.1)
    assert len(triggered) == 1
    assert "空头止损" in triggered[0]["reason"]
    order = seeded.get(Order, triggered[0]["order_id"])
    assert order.side == "buy"  # 买回平仓
    assert order.qty == 2
    seeded.expire_all()
    pos = seeded.scalar(select(Position).where(Position.symbol == CALL))
    assert pos.qty == 0


async def test_guard_short_low_water_trailing(env, seeded):
    from app.risk.guard import run_position_guard

    cfg = seeded.get(RiskConfig, 1)
    cfg.trailing_stop_pct = 25.0
    seeded.commit()
    seeded.add(Position(broker="paper", symbol=CALL, market="US", qty=-1,
                        avg_cost=5.0, multiplier=100))
    seeded.commit()

    env["price"]["p"] = 2.0  # 大幅盈利，压低水位
    assert await run_position_guard() == []
    seeded.expire_all()
    pos = seeded.scalar(select(Position).where(Position.symbol == CALL))
    assert pos.high_water_price == 2.0  # 空头低水位

    env["price"]["p"] = 2.6  # 距 2.0 反弹 30% > 25% → 触发
    triggered = await run_position_guard()
    await asyncio.sleep(0.1)
    assert len(triggered) == 1
    assert "空头移动止损" in triggered[0]["reason"]


async def test_expiry_guard_reminder_and_autoclose(env, seeded):
    from datetime import timedelta

    from app.risk.guard import run_expiry_guard

    near = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y%m%d")
    near_symbol = f"US.AAPL|{near}|C|230"
    seeded.add(Position(broker="paper", symbol=near_symbol, market="US", qty=1,
                        avg_cost=5.0, multiplier=100))
    seeded.commit()

    # 提醒（dte=2 <= 默认3），当日只发一次
    actions = await run_expiry_guard()
    assert len(actions) == 1 and "到期提醒" in actions[0]["reason"]
    assert await run_expiry_guard() == []
    assert any(e.title == "期权临近到期" for e in env["dispatcher"].events)

    # 自动平仓（dte<=1）
    cfg = seeded.get(RiskConfig, 1)
    cfg.auto_close_before_expiry = True
    seeded.commit()
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y%m%d")
    soon_symbol = f"US.AAPL|{tomorrow}|P|200"
    seeded.add(Position(broker="paper", symbol=soon_symbol, market="US", qty=-1,
                        avg_cost=4.0, multiplier=100))
    seeded.commit()
    actions = await run_expiry_guard()
    closes = [a for a in actions if a["order_id"] is not None]
    assert len(closes) == 1
    order = seeded.get(Order, closes[0]["order_id"])
    assert order.side == "buy" and order.symbol == soon_symbol
    # 幂等：同日重跑不再下单
    actions2 = await run_expiry_guard()
    assert [a for a in actions2 if a["order_id"] is not None] == []


# ---------- E2E：TV 期权告警 → paper 成交 ----------


@pytest.fixture
async def client(env, seeded):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        c.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
        yield c


async def test_e2e_tv_option_alert(client, env, seeded):
    from app.bootstrap import get_webhook_token
    from app.config import get_settings
    from app.db.models import StrategyConfig

    seeded.add(StrategyConfig(name="opt_e2e", enabled=True, mode="live",
                              broker="paper", default_qty=1))
    seeded.commit()
    token = get_webhook_token(seeded)
    resp = await client.post(f"/webhook/tradingview/{token}", json={
        "secret": get_settings().webhook_secret, "alert_id": "opt-1",
        "strategy": "opt_e2e", "symbol": "AAPL", "exchange": "NASDAQ",
        "action": "buy", "qty": 1, "order_type": "market", "price": 5.0,
        "expiry": "20991218", "strike": 230, "right": "C",
    })
    assert resp.status_code == 200 and resp.json()["ok"]
    await asyncio.sleep(0.25)
    sig = seeded.scalar(select(Signal).where(Signal.dedup_key == "tv:opt-1"))
    assert sig.symbol == CALL
    order = seeded.scalar(select(Order).where(Order.signal_id == sig.id))
    assert order.status == "filled"
    assert order.multiplier == 100.0
    pos = seeded.scalar(select(Position).where(Position.symbol == CALL))
    assert pos.qty == 1


async def test_manual_order_option_forms(client, env, seeded):
    # 组件形态
    resp = await client.post("/api/manual-order", json={
        "broker": "paper", "symbol": "US.AAPL", "side": "buy", "order_type": "market",
        "qty": 1, "expiry": "2099-12-18", "strike": 230, "right": "C",
    })
    assert resp.status_code == 200
    await asyncio.sleep(0.1)
    # 完整符号形态
    resp = await client.post("/api/manual-order", json={
        "broker": "paper", "symbol": PUT, "side": "buy", "order_type": "market", "qty": 1,
    })
    assert resp.status_code == 200
    # 部分字段 → 400
    resp = await client.post("/api/manual-order", json={
        "broker": "paper", "symbol": "US.AAPL", "side": "buy", "qty": 1, "strike": 230,
    })
    assert resp.status_code == 400
