import re

import httpx

from app.config import get_settings
from app.domain.schemas import NotifyEvent
from app.notify.base import Notifier

_TITLE_MAX = 32  # Server酱 标题长度上限


def build_url(sendkey: str) -> str:
    """按 sendkey 形态选择接口地址。

    Server酱³ 的 key 形如 sctp{uid}t{token}，走用户专属域名；
    其余（Turbo 版）走 sctapi.ftqq.com。
    """
    m = re.match(r"^sctp(\d+)t", sendkey)
    if m:
        return f"https://{m.group(1)}.push.ft07.com/send/{sendkey}.send"
    return f"https://sctapi.ftqq.com/{sendkey}.send"


class ServerChanNotifier(Notifier):
    """Server酱（微信推送）。"""

    type = "serverchan"

    async def send(self, event: NotifyEvent) -> None:
        key = get_settings().serverchan_sendkey
        if not key:
            raise RuntimeError("未配置 SERVERCHAN_SENDKEY")
        title = event.title[:_TITLE_MAX]
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(build_url(key), json={
                "title": title,
                "desp": self.format_body(event) or title,
            })
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") not in (0, None):
                raise RuntimeError(f"Server酱推送失败: {data}")
