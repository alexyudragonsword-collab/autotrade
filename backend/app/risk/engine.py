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

    def check(self, db: Session, intent: OrderIntent) -> RiskDecision:
        try:
            ctx = self._build_context(db, intent)
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

    def _build_context(self, db: Session, intent: OrderIntent) -> RiskContext:
        config = db.get(RiskConfig, 1)
        if config is None:
            raise RuntimeError("风控配置缺失")

        pos = db.scalar(select(Position).where(Position.broker == intent.broker,
                                               Position.symbol == intent.symbol))
        position_qty = pos.qty if pos else 0.0
        est_price = intent.est_price or (pos.avg_cost if pos else 0.0)
        position_value = position_qty * (est_price or 0.0)

        all_pos = db.scalars(select(Position).where(Position.broker == intent.broker)).all()
        total_exposure = sum(p.qty * p.avg_cost for p in all_pos)

        day_start = datetime.combine(datetime.now(timezone.utc).date(), time.min, tzinfo=timezone.utc)
        orders_today = db.scalar(
            select(func.count(Order.id)).where(Order.broker == intent.broker,
                                               Order.created_at >= day_start)) or 0

        # 当日已实现盈亏：当日卖出成交额 - 对应买入成本近似（用 avg_cost 计算）
        realized = 0.0
        sells = db.execute(
            select(TradeFill, Order).join(Order, TradeFill.order_id == Order.id)
            .where(Order.broker == intent.broker, Order.side == "sell", TradeFill.ts >= day_start)
        ).all()
        for fill, order in sells:
            p = db.scalar(select(Position).where(Position.broker == intent.broker,
                                                 Position.symbol == order.symbol))
            cost_basis = p.avg_cost if p and p.avg_cost else fill.price
            realized += (fill.price - cost_basis) * fill.qty - fill.fee

        return RiskContext(
            config=config,
            position_qty=position_qty,
            position_value=position_value,
            total_exposure=total_exposure,
            orders_today=int(orders_today),
            realized_pnl_today=realized,
        )

    def _log(self, db: Session, intent: OrderIntent, decision: RiskDecision) -> None:
        try:
            db.add(RiskEventLog(
                rule_name=decision.rule_name,
                order_intent={
                    "symbol": intent.symbol, "market": intent.market, "side": intent.side,
                    "qty": intent.qty, "est_price": intent.est_price, "broker": intent.broker,
                    "strategy": intent.strategy,
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
