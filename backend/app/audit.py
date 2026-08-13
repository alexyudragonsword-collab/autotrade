"""写操作审计中间件：记录 /api 下的 POST/PUT/DELETE。"""

import json
import logging

import jwt
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models import AuditLog

logger = logging.getLogger(__name__)

_SKIP_PATHS = {"/api/auth/login"}  # 不落密码
_REDACT_KEYS = {"password", "secret", "token", "pwd"}
_MAX_BODY = 500


def _redact(payload) -> str:
    try:
        if isinstance(payload, dict):
            clean = {k: ("***" if any(s in k.lower() for s in _REDACT_KEYS) else v)
                     for k, v in payload.items()}
            return json.dumps(clean, ensure_ascii=False)[:_MAX_BODY]
    except Exception:
        pass
    return ""


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.method not in ("POST", "PUT", "DELETE") or \
                not request.url.path.startswith("/api") or \
                request.url.path in _SKIP_PATHS:
            return await call_next(request)

        body_summary = ""
        try:
            raw = await request.body()
            if raw:
                body_summary = _redact(json.loads(raw))
        except Exception:
            pass

        username = None
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            try:
                payload = jwt.decode(auth[7:], get_settings().jwt_secret, algorithms=["HS256"])
                username = payload.get("sub")
            except jwt.PyJWTError:
                pass

        response = await call_next(request)
        try:
            db = SessionLocal()
            try:
                db.add(AuditLog(
                    username=username, method=request.method,
                    path=request.url.path[:200], body=body_summary or None,
                    status_code=response.status_code,
                    ip=request.client.host if request.client else None,
                ))
                db.commit()
            finally:
                db.close()
        except Exception:
            logger.exception("写审计日志失败")
        return response
