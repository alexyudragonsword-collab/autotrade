"""TradingView 告警解析 → NormalizedSignal。

约定的告警 message 模板（docs/tradingview-setup.md 有完整说明）：
{
  "secret": "<WEBHOOK_SECRET>",
  "alert_id": "{{timenow}}-{{ticker}}-buy",
  "strategy": "my_strategy",
  "symbol": "{{ticker}}",
  "exchange": "{{exchange}}",
  "action": "buy" | "sell" | "close",
  "qty": 100,                # 可选，缺省用策略配置 default_qty
  "order_type": "market" | "limit",   # 可选，默认 market
  "price": {{close}}         # 限价单必填；市价单作为估值参考
}
"""

import hashlib
import time

from app.data.base import normalize_symbol
from app.domain.enums import OrderType, SignalAction
from app.domain.schemas import NormalizedSignal


class SignalParseError(ValueError):
    pass


def _as_float(value, name: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise SignalParseError(f"字段 {name} 不是数字: {value!r}")


def parse_tv_alert(payload: dict) -> NormalizedSignal:
    if not isinstance(payload, dict):
        raise SignalParseError("告警内容必须是 JSON 对象")

    strategy = str(payload.get("strategy") or "").strip()
    if not strategy:
        raise SignalParseError("缺少 strategy 字段（用于匹配策略配置）")

    ticker = str(payload.get("symbol") or "").strip()
    if not ticker:
        raise SignalParseError("缺少 symbol 字段")
    exchange = str(payload.get("exchange") or "").strip()
    try:
        market, symbol = normalize_symbol(exchange, ticker)
    except ValueError as e:
        raise SignalParseError(str(e))

    # 期权字段（可选）：出现任一则三者必填，正股符号映射到指定合约
    expiry_raw = str(payload.get("expiry") or "").strip()
    strike_raw = payload.get("strike")
    right_raw = str(payload.get("right") or "").strip()
    if expiry_raw or strike_raw is not None or right_raw:
        from app.domain.contracts import OptionContract
        from app.domain.enums import Market as _Market

        if not (expiry_raw and strike_raw is not None and right_raw):
            raise SignalParseError("期权信号必须同时提供 expiry / strike / right 三个字段")
        expiry = expiry_raw.replace("-", "")
        if len(expiry) != 8 or not expiry.isdigit():
            raise SignalParseError(f"expiry 必须是 YYYYMMDD 或 YYYY-MM-DD: {expiry_raw!r}")
        right = {"c": "C", "call": "C", "p": "P", "put": "P"}.get(right_raw.lower())
        if right is None:
            raise SignalParseError(f"right 必须是 C/P/call/put: {right_raw!r}")
        strike = _as_float(strike_raw, "strike")
        if strike is None or strike <= 0:
            raise SignalParseError(f"strike 必须为正数: {strike_raw!r}")
        if market == _Market.CN:
            raise SignalParseError("暂不支持 A股期权")
        symbol = OptionContract(underlying=symbol, expiry=expiry,
                                right=right, strike=strike).symbol()

    action_raw = str(payload.get("action") or "").lower().strip()
    try:
        action = SignalAction(action_raw)
    except ValueError:
        raise SignalParseError(f"action 必须是 buy/sell/close: {action_raw!r}")

    order_type_raw = str(payload.get("order_type") or "market").lower().strip()
    try:
        order_type = OrderType(order_type_raw)
    except ValueError:
        raise SignalParseError(f"order_type 必须是 market/limit: {order_type_raw!r}")

    qty = _as_float(payload.get("qty"), "qty")
    if qty is not None and qty <= 0:
        raise SignalParseError(f"qty 必须为正数: {qty}")
    price = _as_float(payload.get("price"), "price")
    if order_type == OrderType.LIMIT and price is None:
        raise SignalParseError("限价单必须提供 price")

    alert_id = str(payload.get("alert_id") or "").strip()
    if alert_id:
        dedup_key = f"tv:{alert_id}"
    else:
        # 未提供 alert_id 时按分钟粒度兜底去重（同一分钟同策略同标的同方向只处理一次）
        bucket = int(time.time() // 60)
        raw = f"{strategy}|{symbol}|{action}|{bucket}"
        dedup_key = "tvh:" + hashlib.sha256(raw.encode()).hexdigest()[:32]

    return NormalizedSignal(
        source="tradingview",
        dedup_key=dedup_key,
        strategy=strategy,
        symbol=symbol,
        market=market,
        action=action,
        quantity=qty,
        order_type=order_type,
        price=price,
        raw={k: v for k, v in payload.items() if k != "secret"},
    )
