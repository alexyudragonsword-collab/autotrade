"""RiskEngine：下单前风控闸门。全部规则通过才放行；任何异常一律拒绝（fail-closed）。"""

import logging
from datetime import datetime, time, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Order, Position, RiskConfig, RiskEventLog, TradeFill
from app.domain.schemas import OrderIntent, RiskDecision
from app.risk.rules import RiskContext, default_rules

logger = logging.getLogger(__name__)


class RiskEngine:
    def __init__(self, rules=None, now_fn=None):
        self.rules = rules if rules is not None else default_rules(now_fn)

    def check(self, db: Session, intent: OrderIntent,
              account_cash: float | None = None) -> RiskDecision:
        """account_cash：期权卖单时由调用方预取（异步侧），None 时回退净值快照 → fail-closed。"""
        try:
            ctx = self._build_context(db, intent, account_cash)
        except Exception:
            logger.exception("风控上下文构建失败，拒绝订单（fail-closed）")
            decision = RiskDecision(False, "risk_engine", "风控内部错误，已拒绝下单")
            self._log(db, intent, decision)
            return decision

        for rule in self.rules:
            try:
                decision = rule.check(intent, ctx)
            except Exception:
                logger.exception("规则 %s 执行异常，拒绝订单（fail-closed）", rule.name)
                decision = RiskDecision(False, rule.name, "规则执行异常，已拒绝下单")
            if not decision.allowed:
                self._log(db, intent, decision)
                return decision
        decision = RiskDecision(True, "all", "全部规则通过")
        self._log(db, intent, decision)
        return decision

    # ---------- 内部 ----------

    def _build_context(self, db: Session, intent: OrderIntent,
                       account_cash: float | None = None) -> RiskContext:
        from app.domain.contracts import OptionContract, underlying_of

        config = db.get(RiskConfig, 1)
        if config is None:
            raise RuntimeError("风控配置缺失")

        pos = db.scalar(select(Position).where(Position.broker == intent.broker,
                                               Position.symbol == intent.symbol))
        position_qty = pos.qty if pos else 0.0
        est_price = intent.est_price or (pos.avg_cost if pos else 0.0)
        position_value = abs(position_qty) * (est_price or 0.0) * (intent.multiplier or 1.0)

        all_pos = db.scalars(select(Position).where(Position.broker == intent.broker)).all()
        total_exposure = sum(abs(p.qty) * p.avg_cost * (p.multiplier or 1.0) for p in all_pos)

        # ---- 期权卖方上下文 ----
        intent_contract = OptionContract.parse(intent.symbol)
        underlying_qty = 0.0
        short_call_qty = 0.0
        short_put_reserved = 0.0
        short_option_notional = 0.0
        for p in all_pos:
            contract = OptionContract.parse(p.symbol)
            if contract is None:
                continue
            if p.qty < 0:
                notional = contract.strike * (p.multiplier or 1.0) * abs(p.qty)
                short_option_notional += notional
                if contract.right == "P":
                    short_put_reserved += notional
                if intent_contract is not None and contract.right == "C" \
                        and contract.underlying == intent_contract.underlying \
                        and p.symbol != intent.symbol:
                    short_call_qty += abs(p.qty)
        if intent_contract is not None:
            stock_pos = next((p for p in all_pos
                              if p.symbol == intent_contract.underlying), None)
            underlying_qty = stock_pos.qty if stock_pos else 0.0

        # 现金：调用方未预取时回退最近净值快照（≤4h 粒度）
        if account_cash is None and intent_contract is not None:
            from app.db.models import AccountValueSnapshot

            snap = db.scalar(select(AccountValueSnapshot)
                             .where(AccountValueSnapshot.broker == intent.broker)
                             .order_by(AccountValueSnapshot.date.desc(),
                                       AccountValueSnapshot.id.desc()))
            account_cash = snap.cash if snap else None

        day_start = datetime.combine(datetime.now(timezone.utc).date(), time.min, tzinfo=timezone.utc)
        orders_today = db.scalar(
            select(func.count(Order.id)).where(Order.broker == intent.broker,
                                               Order.created_at >= day_start)) or 0

        # 当日已实现盈亏：成交时逐笔落库的 realized_pnl 直接累加（见 OrderManager._on_fill）
        realized = db.scalar(
            select(func.coalesce(func.sum(TradeFill.realized_pnl), 0.0))
            .join(Order, TradeFill.order_id == Order.id)
            .where(Order.broker == intent.broker,
                   TradeFill.realized_pnl.is_not(None),
                   TradeFill.ts >= day_start)
        ) or 0.0

        return RiskContext(
            config=config,
            position_qty=position_qty,
            position_value=position_value,
            total_exposure=total_exposure,
            orders_today=int(orders_today),
            realized_pnl_today=realized,
            multiplier=intent.multiplier or 1.0,
            underlying_qty=underlying_qty,
            short_call_qty_same_underlying=short_call_qty,
            short_put_reserved=short_put_reserved,
            short_option_notional=short_option_notional,
            account_cash=account_cash,
        )

    def _log(self, db: Session, intent: OrderIntent, decision: RiskDecision) -> None:
        try:
            db.add(RiskEventLog(
                rule_name=decision.rule_name,
                order_intent={
                    "symbol": intent.symbol, "market": intent.market, "side": intent.side,
                    "qty": intent.qty, "est_price": intent.est_price, "broker": intent.broker,
                    "strategy": intent.strategy, "multiplier": intent.multiplier,
                },
                decision="allow" if decision.allowed else "block",
                reason=decision.reason[:300] if decision.reason else None,
            ))
            db.commit()
        except Exception:
            logger.exception("写风控日志失败")
            db.rollback()


_engine: RiskEngine | None = None


def get_risk_engine() -> RiskEngine:
    global _engine
    if _engine is None:
        _engine = RiskEngine()
    return _engine
