import base64
import hashlib
import hmac
import time

import httpx

from app.config import get_settings
from app.domain.schemas import NotifyEvent
from app.notify.base import Notifier


class LarkNotifier(Notifier):
    """飞书/Lark 群机器人（支持加签）。"""

    type = "lark"

    async def send(self, event: NotifyEvent) -> None:
        s = get_settings()
        url = s.lark_webhook_url
        if not url:
            raise RuntimeError("未配置 LARK_WEBHOOK_URL")
        payload: dict = {"msg_type": "text", "content": {"text": self.format_text(event)}}
        if s.lark_secret:
            # 飞书签名：以 "{timestamp}\n{secret}" 为密钥对空串做 HMAC-SHA256
            ts = str(int(time.time()))
            digest = hmac.new(f"{ts}\n{s.lark_secret}".encode(), digestmod=hashlib.sha256).digest()
            payload["timestamp"] = ts
            payload["sign"] = base64.b64encode(digest).decode()
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            # 新版返回 code，旧版返回 StatusCode，均以 0 为成功
            code = data.get("code", data.get("StatusCode"))
            if code not in (0, None):
                raise RuntimeError(f"飞书推送失败: {data}")
