from datetime import datetime, timezone

from app.db.models import Order, Position, RiskConfig
from app.domain.enums import Market, OrderSide, OrderType
from app.domain.schemas import OrderIntent
from app.risk.engine import RiskEngine
from app.risk.rules import default_rules


def _intent(**overrides) -> OrderIntent:
    base = dict(symbol="US.AAPL", market=Market.US, side=OrderSide.BUY,
                order_type=OrderType.MARKET, qty=10, est_price=100.0, broker="paper")
    base.update(overrides)
    return OrderIntent(**base)


# 固定在美股交易时段内（周三 15:00 UTC = 纽约 10/11 点）
_TRADING_TIME = datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)
_engine = RiskEngine(rules=default_rules(now_fn=lambda: _TRADING_TIME))


def test_allow_normal_order(seeded):
    decision = _engine.check(seeded, _intent())
    assert decision.allowed, decision.reason


def test_kill_switch_blocks(seeded):
    cfg = seeded.get(RiskConfig, 1)
    cfg.trading_enabled = False
    seeded.commit()
    decision = _engine.check(seeded, _intent())
    assert not decision.allowed
    assert decision.rule_name == "kill_switch"


def test_whitelist(seeded):
    cfg = seeded.get(RiskConfig, 1)
    cfg.symbol_whitelist = ["US.TSLA"]
    seeded.commit()
    assert not _engine.check(seeded, _intent(symbol="US.AAPL")).allowed
    assert _engine.check(seeded, _intent(symbol="US.TSLA")).allowed


def test_max_order_value_boundary(seeded):
    cfg = seeded.get(RiskConfig, 1)
    cfg.max_order_value = 1000.0
    seeded.commit()
    # 恰好等于上限 → 放行；超过 → 拦截
    assert _engine.check(seeded, _intent(qty=10, est_price=100.0)).allowed
    decision = _engine.check(seeded, _intent(qty=10, est_price=100.01))
    assert not decision.allowed
    assert decision.rule_name == "max_order_value"


def test_missing_price_blocks(seeded):
    decision = _engine.check(seeded, _intent(est_price=None))
    assert not decision.allowed
    assert decision.rule_name == "max_order_value"


def test_max_position_value(seeded):
    cfg = seeded.get(RiskConfig, 1)
    cfg.max_position_value_per_symbol = 2000.0
    seeded.commit()
    seeded.add(Position(broker="paper", symbol="US.AAPL", market="US", qty=15, avg_cost=100.0))
    seeded.commit()
    # 现持仓 1500 + 新买 1000 = 2500 > 2000 → 拦截
    decision = _engine.check(seeded, _intent(qty=10, est_price=100.0))
    assert not decision.allowed
    assert decision.rule_name == "max_position_value_per_symbol"
    # 卖出不受该规则限制
    assert _engine.check(seeded, _intent(side=OrderSide.SELL, qty=10)).allowed


def test_max_total_exposure(seeded):
    cfg = seeded.get(RiskConfig, 1)
    cfg.max_total_exposure = 3000.0
    seeded.commit()
    seeded.add(Position(broker="paper", symbol="US.TSLA", market="US", qty=25, avg_cost=100.0))
    seeded.commit()
    decision = _engine.check(seeded, _intent(qty=10, est_price=100.0))  # 2500+1000 > 3000
    assert not decision.allowed
    assert decision.rule_name == "max_total_exposure"


def test_max_orders_per_day(seeded):
    cfg = seeded.get(RiskConfig, 1)
    cfg.max_orders_per_day = 2
    seeded.commit()
    for _ in range(2):
        seeded.add(Order(broker="paper", symbol="US.AAPL", market="US", side="buy",
                         order_type="market", qty=1, status="filled"))
    seeded.commit()
    decision = _engine.check(seeded, _intent())
    assert not decision.allowed
    assert decision.rule_name == "max_orders_per_day"


def test_sell_exceeds_position(seeded):
    seeded.add(Position(broker="paper", symbol="US.AAPL", market="US", qty=5, avg_cost=100.0))
    seeded.commit()
    decision = _engine.check(seeded, _intent(side=OrderSide.SELL, qty=10))
    assert not decision.allowed
    assert decision.rule_name == "sell_exceeds_position"
    assert _engine.check(seeded, _intent(side=OrderSide.SELL, qty=5)).allowed


def test_trading_hours(seeded):
    # 周六 → 全市场休市
    weekend = datetime(2026, 8, 15, 15, 0, tzinfo=timezone.utc)
    engine = RiskEngine(rules=default_rules(now_fn=lambda: weekend))
    decision = engine.check(seeded, _intent())
    assert not decision.allowed
    assert decision.rule_name == "trading_hours"
    # 关闭交易时段检查后放行
    cfg = seeded.get(RiskConfig, 1)
    cfg.trading_hours_enabled = False
    seeded.commit()
    assert engine.check(seeded, _intent()).allowed


def test_risk_event_logged(seeded):
    from app.db.models import RiskEventLog

    cfg = seeded.get(RiskConfig, 1)
    cfg.trading_enabled = False
    seeded.commit()
    _engine.check(seeded, _intent())
    events = seeded.query(RiskEventLog).filter(RiskEventLog.decision == "block").all()
    assert len(events) == 1
    assert events[0].rule_name == "kill_switch"
