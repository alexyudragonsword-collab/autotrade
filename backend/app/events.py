"""进程内事件总线：信号/订单/通知事件广播给 WebSocket 订阅者。"""

import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_MAX_QUEUE = 200


class EventBus:
    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event_type: str, data: dict) -> None:
        """同步发布（可在任意协程中调用）；慢消费者丢最旧消息，绝不阻塞业务。"""
        message = {"type": event_type, "ts": datetime.now(timezone.utc).isoformat(), "data": data}
        for q in list(self._subscribers):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(message)
                except Exception:
                    pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
