import httpx

from app.config import get_settings
from app.domain.enums import NotifyLevel
from app.domain.schemas import NotifyEvent
from app.notify.base import Notifier

# error/warn 用 timeSensitive：可穿透 iOS 专注模式，实盘告警不被静音吞掉
_LEVEL_MAP = {
    NotifyLevel.ERROR: "timeSensitive",
    NotifyLevel.WARN: "timeSensitive",
    NotifyLevel.INFO: "active",
}


class BarkNotifier(Notifier):
    """Bark（iOS 推送）。BARK_URL 填含 key 的完整地址，如 https://api.day.app/yourkey"""

    type = "bark"

    async def send(self, event: NotifyEvent) -> None:
        url = get_settings().bark_url
        if not url:
            raise RuntimeError("未配置 BARK_URL")
        payload = {
            "title": event.title,
            "body": self.format_body(event) or event.title,
            "group": "AutoTrade",
            "level": _LEVEL_MAP.get(event.level, "active"),
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url.rstrip("/"), json=payload)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") not in (200, 0, None):
                raise RuntimeError(f"Bark 推送失败: {data}")
