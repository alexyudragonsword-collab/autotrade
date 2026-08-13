"""首次启动初始化：管理员用户、风控配置单行、Webhook token。"""

import logging
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import hash_password
from app.config import get_settings
from app.db.models import AppSetting, RiskConfig, User

logger = logging.getLogger(__name__)

WEBHOOK_TOKEN_KEY = "webhook_token"


def seed_all(db: Session) -> None:
    settings = get_settings()

    user = db.scalar(select(User).where(User.username == settings.admin_username))
    if user is None:
        db.add(User(username=settings.admin_username, password_hash=hash_password(settings.admin_password)))
        logger.info("已创建管理员用户 %s", settings.admin_username)

    if db.get(RiskConfig, 1) is None:
        db.add(RiskConfig(id=1))
        logger.info("已创建默认风控配置")

    if db.get(AppSetting, WEBHOOK_TOKEN_KEY) is None:
        token = secrets.token_urlsafe(32)
        db.add(AppSetting(key=WEBHOOK_TOKEN_KEY, value=token))
        logger.info("已生成 TradingView Webhook token（见 设置 页面）")

    db.commit()


def get_webhook_token(db: Session) -> str:
    row = db.get(AppSetting, WEBHOOK_TOKEN_KEY)
    return row.value if row else ""


def rotate_webhook_token(db: Session) -> str:
    token = secrets.token_urlsafe(32)
    row = db.get(AppSetting, WEBHOOK_TOKEN_KEY)
    if row is None:
        row = AppSetting(key=WEBHOOK_TOKEN_KEY, value=token)
        db.add(row)
    else:
        row.value = token
    db.commit()
    return token
