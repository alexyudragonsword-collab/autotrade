"""期权链 API：到期日列表与行权价矩阵（从已连接券商实时拉取）。"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_current_user
from app.brokers.base import BrokerError
from app.brokers.manager import get_broker_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/options", tags=["options"],
                   dependencies=[Depends(get_current_user)])


def _adapter(broker: str):
    adapter = get_broker_manager().get_if_connected(broker)
    if adapter is None:
        raise HTTPException(400, f"账户 {broker} 未连接")
    return adapter


@router.get("/expirations")
async def option_expirations(broker: str, underlying: str):
    try:
        expirations = await _adapter(broker).get_option_expirations(underlying.strip())
    except BrokerError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("查询期权到期日失败")
        raise HTTPException(500, f"查询失败: {e}")
    return {"underlying": underlying, "expirations": expirations}


@router.get("/chain")
async def option_chain(broker: str, underlying: str, expiry: str,
                       with_quotes: bool = True, strikes_around: int = 20):
    from app.domain.contracts import default_multiplier

    underlying = underlying.strip()
    try:
        items = await _adapter(broker).get_option_chain(
            underlying, expiry, with_quotes=with_quotes,
            strikes_around=min(max(strikes_around, 1), 60))
    except BrokerError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("查询期权链失败")
        raise HTTPException(500, f"查询失败: {e}")

    # 标的现价（ATM 高亮用）
    underlying_price = None
    try:
        quote = await _adapter(broker).get_quote(underlying)
        if quote is not None:
            underlying_price = quote.price
    except Exception:
        pass

    rows: dict[float, dict] = {}
    multiplier = None
    for item in items:
        entry = {"symbol": item.symbol, "bid": item.bid, "ask": item.ask,
                 "last": item.last, "open_interest": item.open_interest,
                 "multiplier": item.multiplier}
        multiplier = multiplier or item.multiplier
        row = rows.setdefault(item.strike, {"strike": item.strike, "call": None, "put": None})
        row["call" if item.right == "C" else "put"] = entry

    return {
        "underlying": underlying,
        "underlying_price": underlying_price,
        "expiry": expiry,
        "multiplier": multiplier or default_multiplier(f"{underlying}|{expiry}|C|1"),
        "rows": sorted(rows.values(), key=lambda r: r["strike"]),
    }
