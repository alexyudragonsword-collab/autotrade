import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import create_access_token, get_db, verify_password
from app.db.models import User
from app.domain.schemas import LoginRequest, TokenResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 简单内存登录限速：5 次失败后锁 60 秒
_failures: dict[str, list[float]] = {}
_MAX_FAILS, _WINDOW = 5, 60.0


def _rate_limited(key: str) -> bool:
    now = time.monotonic()
    fails = [t for t in _failures.get(key, []) if now - t < _WINDOW]
    _failures[key] = fails
    return len(fails) >= _MAX_FAILS


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    if _rate_limited(req.username):
        raise HTTPException(429, "尝试次数过多，请稍后再试")
    user = db.scalar(select(User).where(User.username == req.username))
    if user is None or not verify_password(req.password, user.password_hash):
        _failures.setdefault(req.username, []).append(time.monotonic())
        raise HTTPException(401, "用户名或密码错误")
    return TokenResponse(access_token=create_access_token(user.username))
