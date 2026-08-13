import httpx

from app.config import get_settings
from app.domain.schemas import NotifyEvent
from app.notify.base import Notifier


class WecomNotifier(Notifier):
    """企业微信群机器人。"""

    type = "wecom"

    async def send(self, event: NotifyEvent) -> None:
        url = get_settings().wecom_webhook_url
        if not url:
            raise RuntimeError("未配置 WECOM_WEBHOOK_URL")
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "msgtype": "text",
                "text": {"content": self.format_text(event)},
            })
            resp.raise_for_status()
            data = resp.json()
            if data.get("errcode") not in (0, None):
                raise RuntimeError(f"企业微信推送失败: {data}")
