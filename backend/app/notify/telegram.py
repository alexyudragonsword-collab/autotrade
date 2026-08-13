import httpx

from app.config import get_settings
from app.domain.schemas import NotifyEvent
from app.notify.base import Notifier


class TelegramNotifier(Notifier):
    type = "telegram"

    async def send(self, event: NotifyEvent) -> None:
        settings = get_settings()
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            raise RuntimeError("未配置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "chat_id": settings.telegram_chat_id,
                "text": self.format_text(event),
            })
            resp.raise_for_status()
