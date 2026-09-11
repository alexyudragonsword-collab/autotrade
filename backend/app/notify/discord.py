import httpx

from app.config import get_settings
from app.domain.schemas import NotifyEvent
from app.notify.base import Notifier

# Discord 单条消息上限 2000 字符，留余量截断
_MAX_CONTENT = 1900


class DiscordNotifier(Notifier):
    """Discord 频道 Webhook。"""

    type = "discord"

    async def send(self, event: NotifyEvent) -> None:
        url = get_settings().discord_webhook_url
        if not url:
            raise RuntimeError("未配置 DISCORD_WEBHOOK_URL")
        text = self.format_text(event)
        if len(text) > _MAX_CONTENT:
            text = text[:_MAX_CONTENT] + "…"
        async with httpx.AsyncClient(timeout=10) as client:
            # 成功返回 204 无响应体，异常由 raise_for_status 抛出
            resp = await client.post(url, json={"content": text})
            resp.raise_for_status()
