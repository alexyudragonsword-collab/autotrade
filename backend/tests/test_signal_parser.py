import pytest

from app.domain.enums import Market, OrderType, SignalAction
from app.signals.parser import SignalParseError, parse_tv_alert


def _payload(**overrides):
    base = {
        "secret": "x", "alert_id": "a1", "strategy": "s1",
        "symbol": "AAPL", "exchange": "NASDAQ", "action": "buy",
        "qty": 10, "order_type": "market", "price": 230.5,
    }
    base.update(overrides)
    return base


def test_parse_us():
    sig = parse_tv_alert(_payload())
    assert sig.market == Market.US
    assert sig.symbol == "US.AAPL"
    assert sig.action == SignalAction.BUY
    assert sig.quantity == 10
    assert sig.dedup_key == "tv:a1"
    assert "secret" not in sig.raw


def test_parse_hk_padding():
    sig = parse_tv_alert(_payload(symbol="700", exchange="HKEX"))
    assert sig.market == Market.HK
    assert sig.symbol == "HK.00700"


def test_parse_cn():
    assert parse_tv_alert(_payload(symbol="600519", exchange="SSE")).symbol == "SH.600519"
    assert parse_tv_alert(_payload(symbol="000001", exchange="SZSE")).symbol == "SZ.000001"


def test_internal_format_passthrough():
    sig = parse_tv_alert(_payload(symbol="US.TSLA", exchange=""))
    assert sig.symbol == "US.TSLA"
    assert sig.market == Market.US


def test_missing_strategy():
    with pytest.raises(SignalParseError, match="strategy"):
        parse_tv_alert(_payload(strategy=""))


def test_bad_action():
    with pytest.raises(SignalParseError, match="action"):
        parse_tv_alert(_payload(action="hold"))


def test_bad_qty_type():
    with pytest.raises(SignalParseError, match="qty"):
        parse_tv_alert(_payload(qty="abc"))
    with pytest.raises(SignalParseError, match="qty"):
        parse_tv_alert(_payload(qty=-5))


def test_qty_string_number_ok():
    assert parse_tv_alert(_payload(qty="15")).quantity == 15.0


def test_limit_requires_price():
    with pytest.raises(SignalParseError, match="限价"):
        parse_tv_alert(_payload(order_type="limit", price=None))
    sig = parse_tv_alert(_payload(order_type="limit", price=100))
    assert sig.order_type == OrderType.LIMIT


def test_unknown_exchange():
    with pytest.raises(SignalParseError):
        parse_tv_alert(_payload(exchange="MOEX", symbol="GAZP"))


def test_no_alert_id_fallback_hash():
    sig1 = parse_tv_alert(_payload(alert_id=""))
    sig2 = parse_tv_alert(_payload(alert_id=""))
    assert sig1.dedup_key.startswith("tvh:")
    assert sig1.dedup_key == sig2.dedup_key  # 同分钟同内容 → 同 key
