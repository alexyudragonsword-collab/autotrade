import asyncio

import pytest

from app.brokers.paper import PaperBroker
from app.domain.enums import Market, OrderSide, OrderStatus, OrderType
from app.domain.schemas import OrderRequest


@pytest.fixture
async def broker(seeded):
    async def quote(symbol):
        return 100.0

    b = PaperBroker(quote_fn=quote, fee_bps=0)
    await b.connect()
    return b


def _req(**overrides) -> OrderRequest:
    base = dict(symbol="US.AAPL", market=Market.US, side=OrderSide.BUY,
                order_type=OrderType.MARKET, qty=10)
    base.update(overrides)
    return OrderRequest(**base)


async def _collect_events(broker):
    updates, fills = [], []

    async def on_update(u):
        updates.append(u)

    async def on_fill(f):
        fills.append(f)

    broker.on_order_update(on_update)
    broker.on_fill(on_fill)
    return updates, fills


async def test_market_order_fills(broker):
    updates, fills = await _collect_events(broker)
    ref = await broker.place_order(_req())
    await asyncio.sleep(0.05)
    assert len(fills) == 1
    assert fills[0].price == 100.0
    assert updates[-1].status == OrderStatus.FILLED
    positions = await broker.get_positions()
    assert positions[0].qty == 10
    assert positions[0].avg_cost == 100.0
    account = await broker.get_account()
    assert account.cash == pytest.approx(1_000_000 - 1000)


async def test_sell_updates_cash_and_position(broker):
    await broker.place_order(_req(qty=10))
    await asyncio.sleep(0.05)
    await broker.place_order(_req(side=OrderSide.SELL, qty=4))
    await asyncio.sleep(0.05)
    positions = await broker.get_positions()
    assert positions[0].qty == 6
    account = await broker.get_account()
    assert account.cash == pytest.approx(1_000_000 - 1000 + 400)


async def test_limit_order_pending_then_triggered(seeded):
    price_holder = {"p": 105.0}

    async def quote(symbol):
        return price_holder["p"]

    broker = PaperBroker(quote_fn=quote, fee_bps=0)
    await broker.connect()
    updates, fills = await _collect_events(broker)

    await broker.place_order(_req(order_type=OrderType.LIMIT, limit_price=100.0))
    await asyncio.sleep(0.05)
    assert updates[-1].status == OrderStatus.SUBMITTED
    await broker.check_pending_limits()
    assert not fills  # 105 > 100，买入限价未触发

    price_holder["p"] = 99.0
    await broker.check_pending_limits()
    await asyncio.sleep(0.05)
    assert len(fills) == 1
    assert fills[0].price == 100.0  # 按限价成交


async def test_cancel_pending_limit(seeded):
    async def quote(symbol):
        return 105.0

    broker = PaperBroker(quote_fn=quote)
    await broker.connect()
    updates, _ = await _collect_events(broker)
    ref = await broker.place_order(_req(order_type=OrderType.LIMIT, limit_price=100.0))
    await asyncio.sleep(0.05)
    await broker.cancel_order(ref.broker_order_id)
    assert updates[-1].status == OrderStatus.CANCELLED


async def test_partial_fill(seeded):
    async def quote(symbol):
        return 100.0

    broker = PaperBroker(quote_fn=quote, partial_fill_ratio=0.5)
    await broker.connect()
    updates, fills = await _collect_events(broker)
    await broker.place_order(_req(qty=10))
    await asyncio.sleep(0.05)
    assert fills[0].qty == 5
    assert updates[-1].status == OrderStatus.PARTIALLY_FILLED


async def test_market_order_no_price_rejected(seeded):
    broker = PaperBroker(quote_fn=None)  # 无行情源且 BarStore 无缓存
    await broker.connect()
    updates, _ = await _collect_events(broker)
    await broker.place_order(_req(symbol="US.NOPRICE"))
    await asyncio.sleep(0.05)
    assert updates[-1].status == OrderStatus.REJECTED
