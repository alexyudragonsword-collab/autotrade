import base64
import hashlib
import hmac
import time
import urllib.parse

import httpx

from app.config import get_settings
from app.domain.schemas import NotifyEvent
from app.notify.base import Notifier


class DingtalkNotifier(Notifier):
    """钉钉群机器人（支持加签）。"""

    type = "dingtalk"

    async def send(self, event: NotifyEvent) -> None:
        s = get_settings()
        url = s.dingtalk_webhook_url
        if not url:
            raise RuntimeError("未配置 DINGTALK_WEBHOOK_URL")
        if s.dingtalk_secret:
            ts = str(round(time.time() * 1000))
            sign_str = f"{ts}\n{s.dingtalk_secret}"
            sign = base64.b64encode(
                hmac.new(s.dingtalk_secret.encode(), sign_str.encode(), hashlib.sha256).digest()
            )
            url += f"&timestamp={ts}&sign={urllib.parse.quote_plus(sign)}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "msgtype": "text",
                "text": {"content": self.format_text(event)},
            })
            resp.raise_for_status()
            data = resp.json()
            if data.get("errcode") not in (0, None):
                raise RuntimeError(f"钉钉推送失败: {data}")
