"""TradingView Webhook 接收端点。

安全：URL token（AppSetting 随机生成）+ body secret（env）双重校验，
可选 TV 出口 IP 白名单。端点秒回 200，处理异步化（TV 3 秒超时会重试）。
"""

import asyncio
import logging
import secrets as pysecrets

from fastapi import APIRouter, HTTPException, Request

from app.bootstrap import get_webhook_token
from app.config import get_settings
from app.db.base import SessionLocal
from app.signals.parser import SignalParseError, parse_tv_alert
from app.signals.pipeline import get_pipeline

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhook"])

_MAX_BODY = 16 * 1024


@router.post("/webhook/tradingview/{token}")
async def tradingview_webhook(token: str, request: Request):
    settings = get_settings()

    if settings.tv_ip_allowlist_enabled:
        client_ip = request.client.host if request.client else ""
        if client_ip not in settings.tv_ip_allowlist:
            raise HTTPException(403, "来源 IP 不在白名单")

    db = SessionLocal()
    try:
        expected = get_webhook_token(db)
    finally:
        db.close()
    if not expected or not pysecrets.compare_digest(token, expected):
        raise HTTPException(403, "token 无效")

    body = await request.body()
    if len(body) > _MAX_BODY:
        raise HTTPException(413, "请求体过大")
    try:
        import json

        payload = json.loads(body)
    except Exception:
        raise HTTPException(400, "请求体不是合法 JSON")

    if not pysecrets.compare_digest(str(payload.get("secret", "")), settings.webhook_secret):
        raise HTTPException(403, "secret 无效")

    try:
        normalized = parse_tv_alert(payload)
    except SignalParseError as e:
        logger.warning("TV 告警解析失败: %s", e)
        # 返回 200 避免 TV 反复重发一条永远解析不了的告警
        return {"ok": False, "error": str(e)}

    asyncio.create_task(_process_safe(normalized))
    return {"ok": True}


async def _process_safe(normalized) -> None:
    try:
        await get_pipeline().process(normalized)
    except Exception:
        logger.exception("信号处理异常: %s", normalized.dedup_key)
