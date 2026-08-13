"""信号处理主干：落库(幂等) → 策略匹配 → 风控 → 执行 → 通知。"""

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.base import SessionLocal
from app.db.models import Position, Signal, StrategyConfig
from app.domain.enums import NotifyLevel, OrderSide, SignalAction, SignalStatus, StrategyMode
from app.domain.schemas import NormalizedSignal, NotifyEvent, OrderIntent
from app.execution.order_manager import get_order_manager
from app.notify.dispatcher import get_dispatcher
from app.risk.engine import get_risk_engine

logger = logging.getLogger(__name__)


class SignalPipeline:
    async def process(self, normalized: NormalizedSignal) -> Signal | None:
        """处理一条标准化信号。返回落库后的 Signal；重复信号返回 None。"""
        db = SessionLocal()
        try:
            sig = Signal(
                source=normalized.source, raw_payload=normalized.raw,
                dedup_key=normalized.dedup_key, strategy_name=normalized.strategy,
                symbol=normalized.symbol, market=normalized.market,
                action=normalized.action, quantity=normalized.quantity,
                order_type=normalized.order_type, price=normalized.price,
                status=SignalStatus.RECEIVED,
            )
            db.add(sig)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                logger.info("重复信号已忽略: %s", normalized.dedup_key)
                return None
            db.refresh(sig)

            await self._handle(db, sig, normalized)
            db.refresh(sig)
            from app.events import get_event_bus

            get_event_bus().publish("signal", {
                "id": sig.id, "source": sig.source, "strategy": sig.strategy_name,
                "symbol": sig.symbol, "action": sig.action, "quantity": sig.quantity,
                "status": sig.status, "reject_reason": sig.reject_reason,
            })
            return sig
        finally:
            db.close()

    async def _handle(self, db, sig: Signal, normalized: NormalizedSignal) -> None:
        dispatcher = get_dispatcher()
        strategy = db.scalar(select(StrategyConfig).where(StrategyConfig.name == normalized.strategy))
        if strategy is None or not strategy.enabled:
            reason = "策略未配置" if strategy is None else "策略已停用"
            self._reject(db, sig, SignalStatus.REJECTED, reason)
            await dispatcher.emit(self._event(sig, f"信号被拒绝：{reason}", NotifyLevel.WARN))
            return

        # signal_only：只记录与提醒
        if strategy.mode == StrategyMode.SIGNAL_ONLY:
            sig.status = SignalStatus.EXECUTED
            sig.reject_reason = None
            db.commit()
            await dispatcher.emit(self._event(sig, "收到交易信号（仅提醒模式，未下单）", NotifyLevel.INFO, broker=strategy.broker))
            return

        # live：确定数量与方向（CLOSE 同时支持平多仓与买回空仓）
        qty = normalized.quantity or strategy.default_qty
        side = OrderSide.BUY if normalized.action == SignalAction.BUY else OrderSide.SELL
        if normalized.action == SignalAction.CLOSE:
            pos = db.scalar(select(Position).where(Position.broker == strategy.broker,
                                                   Position.symbol == normalized.symbol))
            pos_qty = pos.qty if pos else 0.0
            qty = abs(pos_qty)
            side = OrderSide.SELL if pos_qty > 0 else OrderSide.BUY
        if not qty or qty <= 0:
            reason = "无可执行数量（信号未带 qty 且策略未配置 default_qty，或平仓时无持仓）"
            self._reject(db, sig, SignalStatus.REJECTED, reason)
            await dispatcher.emit(self._event(sig, f"信号被拒绝：{reason}", NotifyLevel.WARN,
                                              broker=strategy.broker))
            return

        est_price = normalized.price
        if est_price is None:
            est_price = await self._estimate_price(strategy.broker, normalized.symbol)

        multiplier = await resolve_multiplier(strategy.broker, normalized.symbol)
        intent = OrderIntent(
            symbol=normalized.symbol, market=normalized.market, side=side,
            order_type=normalized.order_type, qty=qty, est_price=est_price,
            broker=strategy.broker, strategy=strategy.name, multiplier=multiplier,
        )

        account_cash = await prefetch_cash_if_option_sell(intent)
        decision = get_risk_engine().check(db, intent, account_cash=account_cash)
        if not decision.allowed:
            self._reject(db, sig, SignalStatus.REJECTED_RISK, f"[{decision.rule_name}] {decision.reason}")
            await dispatcher.emit(self._event(sig, f"风控拦截：{decision.reason}", NotifyLevel.WARN, broker=strategy.broker))
            return

        order = await get_order_manager().submit(intent, signal_id=sig.id)
        if order.status == "failed":
            sig.status = SignalStatus.FAILED
            sig.reject_reason = order.error_msg
        else:
            sig.status = SignalStatus.ROUTED
        db.commit()
        await dispatcher.emit(self._event(
            sig, f"信号已提交 {strategy.broker} 下单（订单 #{order.id}）", NotifyLevel.INFO,
            broker=strategy.broker))

    # ---------- 辅助 ----------

    @staticmethod
    def _reject(db, sig: Signal, status: str, reason: str) -> None:
        sig.status = status
        sig.reject_reason = reason[:300]
        db.commit()

    @staticmethod
    def _event(sig: Signal, body: str, level: NotifyLevel, broker: str | None = None) -> NotifyEvent:
        action_cn = {"buy": "买入", "sell": "卖出", "close": "平仓"}.get(sig.action, sig.action)
        return NotifyEvent(level=level, title="交易信号", body=body, fields={
            "策略": sig.strategy_name, "标的": sig.symbol, "动作": action_cn,
            "数量": sig.quantity or "-", "价格": sig.price or "市价",
        }, strategy=sig.strategy_name, broker=broker)

    @staticmethod
    async def _estimate_price(broker: str, symbol: str) -> float | None:
        from app.brokers.manager import get_broker_manager

        adapter = get_broker_manager().get_if_connected(broker)
        if adapter is not None:
            quote = await adapter.get_quote(symbol)
            if quote is not None:
                return quote.price
        return None


async def resolve_multiplier(broker: str, symbol: str) -> float:
    """期权合约乘数：优先问券商快照持仓/链缓存不可得时用市场默认值；股票恒为 1。"""
    from app.domain.contracts import default_multiplier, is_option

    if not is_option(symbol):
        return 1.0
    from app.brokers.manager import get_broker_manager

    adapter = get_broker_manager().get_if_connected(broker)
    if adapter is not None:
        getter = getattr(adapter, "get_contract_multiplier", None)
        if getter is not None:
            try:
                mult = await getter(symbol)
                if mult:
                    return float(mult)
            except Exception:
                logger.debug("查询 %s 合约乘数失败，用默认值", symbol)
    return default_multiplier(symbol)


async def prefetch_cash_if_option_sell(intent: OrderIntent) -> float | None:
    """期权卖单预取账户现金供现金担保 PUT 校验；其它情况返回 None（规则内自行兜底）。"""
    import asyncio

    from app.domain.contracts import is_option

    if not (is_option(intent.symbol) and intent.side == OrderSide.SELL):
        return None
    from app.brokers.manager import get_broker_manager

    adapter = get_broker_manager().get_if_connected(intent.broker)
    if adapter is None:
        return None
    try:
        account = await asyncio.wait_for(adapter.get_account(), timeout=5)
        return account.cash
    except Exception:
        logger.warning("预取 %s 账户现金失败，风控回退净值快照", intent.broker)
        return None


_pipeline: SignalPipeline | None = None


def get_pipeline() -> SignalPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = SignalPipeline()
    return _pipeline
