"""通知分发：按渠道启用状态与最低级别广播；单渠道失败不影响其它渠道。"""

import logging

from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import NotifyChannel, Order
from app.domain.enums import NotifyLevel
from app.domain.schemas import NotifyEvent
from app.notify.base import Notifier
from app.notify.dingtalk import DingtalkNotifier
from app.notify.email import EmailNotifier
from app.notify.telegram import TelegramNotifier
from app.notify.wecom import WecomNotifier

logger = logging.getLogger(__name__)

_NOTIFIERS: dict[str, Notifier] = {
    n.type: n for n in (TelegramNotifier(), EmailNotifier(), WecomNotifier(), DingtalkNotifier())
}


def channel_matches(ch: NotifyChannel, event: NotifyEvent) -> bool:
    """级别 + 路由过滤。config: {"strategies": [...], "brokers": [...]}，空列表 = 不限。

    事件缺少对应元数据（系统级事件，如 kill switch）时视为匹配所有渠道。
    """
    if event.level.rank < NotifyLevel(ch.min_level).rank:
        return False
    cfg = ch.config or {}
    strategies = cfg.get("strategies") or []
    if strategies and event.strategy is not None and event.strategy not in strategies:
        return False
    brokers = cfg.get("brokers") or []
    if brokers and event.broker is not None and event.broker not in brokers:
        return False
    return True


class NotifyDispatcher:
    async def emit(self, event: NotifyEvent) -> None:
        # 广播到 WebSocket（浏览器即时弹窗），与外部渠道互不影响
        try:
            from app.events import get_event_bus

            get_event_bus().publish("notify", {
                "level": event.level, "title": event.title, "body": event.body,
                "fields": event.fields, "strategy": event.strategy, "broker": event.broker,
            })
        except Exception:
            logger.debug("事件总线广播失败", exc_info=True)
        db = SessionLocal()
        try:
            channels = db.scalars(select(NotifyChannel).where(NotifyChannel.enabled)).all()
        finally:
            db.close()
        for ch in channels:
            if not channel_matches(ch, event):
                continue
            notifier = _NOTIFIERS.get(ch.type)
            if notifier is None:
                continue
            try:
                await notifier.send(event)
            except Exception as e:
                logger.warning("通知渠道 %s 发送失败: %s", ch.type, e)

    async def test_channel(self, channel_type: str) -> None:
        notifier = _NOTIFIERS.get(channel_type)
        if notifier is None:
            raise ValueError(f"未知通知渠道: {channel_type}")
        await notifier.send(NotifyEvent(
            level=NotifyLevel.INFO, title="测试通知",
            body="AutoTrade 通知渠道配置成功 ✅",
        ))


def order_event(order: Order, level: str = "info") -> NotifyEvent:
    title = {
        "filled": "订单成交", "partially_filled": "订单部分成交",
        "cancelled": "订单已撤销", "rejected": "订单被拒绝", "failed": "订单失败",
    }.get(order.status, f"订单状态: {order.status}")
    side = "买入" if order.side == "buy" else "卖出"
    fields = {
        "标的": order.symbol, "方向": side, "数量": order.qty,
        "券商": order.broker, "状态": order.status,
    }
    if order.filled_qty:
        fields["成交量"] = order.filled_qty
    if order.avg_fill_price:
        fields["成交均价"] = order.avg_fill_price
    if order.error_msg:
        fields["原因"] = order.error_msg
    return NotifyEvent(level=NotifyLevel(level), title=title, body="", fields=fields,
                       broker=order.broker)


_dispatcher: NotifyDispatcher | None = None


def get_dispatcher() -> NotifyDispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = NotifyDispatcher()
    return _dispatcher


def seed_channels_from_env(db) -> None:
    """启动时：env 配置了的渠道若 DB 无记录则自动建行（enabled）。"""
    from app.config import get_settings

    s = get_settings()
    available = {
        "telegram": bool(s.telegram_bot_token and s.telegram_chat_id),
        "email": bool(s.smtp_host and s.smtp_to),
        "wecom": bool(s.wecom_webhook_url),
        "dingtalk": bool(s.dingtalk_webhook_url),
    }
    existing = {c.type for c in db.scalars(select(NotifyChannel)).all()}
    for ctype, ok in available.items():
        if ok and ctype not in existing:
            db.add(NotifyChannel(type=ctype, name=ctype, enabled=True, min_level="info"))
    db.commit()
