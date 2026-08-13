"""WebSocket 实时推送端点。

前端连接 /ws?token=<JWT>；服务器把事件总线上的消息（信号/订单/通知）
推送给所有已认证连接。浏览器 WebSocket 无法带自定义 header，故 token 走查询参数。
"""

import asyncio
import logging

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import get_settings
from app.events import get_event_bus

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = ""):
    try:
        jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        await ws.close(code=4401)
        return
    await ws.accept()
    bus = get_event_bus()
    queue = bus.subscribe()
    try:
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=25)
                await ws.send_json(message)
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping"})  # 保活
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bus.unsubscribe(queue)
