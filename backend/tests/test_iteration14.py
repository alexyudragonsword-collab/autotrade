"""迭代14：通知渠道扩充、指标扩充、日志筛选与导出。"""

import numpy as np
import pandas as pd
import pytest

from app.domain.enums import NotifyLevel
from app.domain.schemas import NotifyEvent
from app.notify.bark import BarkNotifier
from app.notify.base import Notifier
from app.notify.discord import DiscordNotifier
from app.notify.dispatcher import CHANNEL_TYPES, _NOTIFIERS
from app.notify.lark import LarkNotifier
from app.notify.serverchan import ServerChanNotifier, build_url
from app.screener.expressions import eval_expr_last, validate_expr
from app.screener.indicators import (
    BBWIDTH,
    INDICATOR_FUNCS,
    KDJ_D,
    KDJ_J,
    KDJ_K,
    OBV,
    VWAP,
)


# ---------- 通知渠道 ----------


def test_新渠道全部注册():
    for t in ("lark", "discord", "bark", "serverchan"):
        assert t in _NOTIFIERS
        assert t in CHANNEL_TYPES
    # 原有渠道未被挤掉
    for t in ("telegram", "email", "wecom", "dingtalk"):
        assert t in CHANNEL_TYPES


def test_未配置时各渠道报错而非静默失败(monkeypatch):
    from app.config import get_settings

    get_settings.cache_clear()
    event = NotifyEvent(level=NotifyLevel.INFO, title="t", body="b")
    for notifier in (LarkNotifier(), DiscordNotifier(), BarkNotifier(), ServerChanNotifier()):
        with pytest.raises(RuntimeError, match="未配置"):
            import asyncio

            asyncio.run(notifier.send(event))


def test_format_body_不含标题():
    event = NotifyEvent(level=NotifyLevel.INFO, title="订单成交", body="正文",
                        fields={"标的": "US.AAPL", "数量": 100})
    body = Notifier.format_body(event)
    assert "订单成交" not in body
    assert "正文" in body and "US.AAPL" in body
    # format_text 仍保留标题（既有渠道行为不变）
    assert "【订单成交】" in Notifier.format_text(event)


def test_serverchan_按key形态选择接口():
    assert build_url("SCT123456abc").startswith("https://sctapi.ftqq.com/")
    # Server酱³ 的 key 形如 sctp{uid}t{token}，走用户专属域名
    url3 = build_url("sctp98765tXXXXYYYY")
    assert url3 == "https://98765.push.ft07.com/send/sctp98765tXXXXYYYY.send"


def test_discord_超长消息被截断(monkeypatch):
    import asyncio

    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            captured["json"] = json
            return _Resp()

    monkeypatch.setattr("app.notify.discord.httpx.AsyncClient", lambda **kw: _Client())
    monkeypatch.setattr("app.notify.discord.get_settings",
                        lambda: type("S", (), {"discord_webhook_url": "https://x/y"})())
    event = NotifyEvent(level=NotifyLevel.INFO, title="t", body="x" * 5000)
    asyncio.run(DiscordNotifier().send(event))
    assert len(captured["json"]["content"]) <= 1901


def test_bark_错误级别用timeSensitive穿透勿扰(monkeypatch):
    import asyncio

    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"code": 200}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            captured["json"] = json
            return _Resp()

    monkeypatch.setattr("app.notify.bark.httpx.AsyncClient", lambda **kw: _Client())
    monkeypatch.setattr("app.notify.bark.get_settings",
                        lambda: type("S", (), {"bark_url": "https://api.day.app/key"})())
    asyncio.run(BarkNotifier().send(
        NotifyEvent(level=NotifyLevel.ERROR, title="风控拦截", body="超限")))
    assert captured["json"]["level"] == "timeSensitive"
    asyncio.run(BarkNotifier().send(
        NotifyEvent(level=NotifyLevel.INFO, title="日报", body="正常")))
    assert captured["json"]["level"] == "active"


# ---------- 技术指标 ----------


@pytest.fixture
def bars():
    close = pd.Series([10, 11, 12, 11, 13, 14, 13, 15, 16, 15, 17, 18, 17, 19, 20], dtype=float)
    return pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": pd.Series([1000] * len(close), dtype=float),
    })


def test_新指标全部注册进白名单():
    for name in ("KDJ_K", "KDJ_D", "KDJ_J", "OBV", "VWAP", "BBWIDTH"):
        assert name in INDICATOR_FUNCS


def test_kdj_三线关系与取值范围(bars):
    k = KDJ_K(bars["high"], bars["low"], bars["close"])
    d = KDJ_D(bars["high"], bars["low"], bars["close"])
    j = KDJ_J(bars["high"], bars["low"], bars["close"])
    assert np.allclose(j, 3 * k - 2 * d)      # J 的定义
    assert k.between(0, 100).all()            # K/D 恒在 0~100
    assert d.between(0, 100).all()
    assert k.notna().all() and d.notna().all()


def test_kdj_横盘取中性值50不产生NaN():
    flat = pd.Series([5.0] * 15)
    k = KDJ_K(flat, flat, flat)
    assert k.notna().all()
    assert k.iloc[-1] == pytest.approx(50.0)  # 区间为零 → RSV 取 50


def test_obv_按收盘方向累计(bars):
    obv = OBV(bars["close"], bars["volume"])
    # 首根无前值计 0；之后涨加、跌减
    assert obv.iloc[0] == 0
    assert obv.iloc[1] == 1000      # 10 → 11 涨
    assert obv.iloc[3] == 1000      # 12 → 11 跌，回吐一份
    # 平盘不计
    flat_close = pd.Series([10.0, 10.0, 10.0])
    flat_vol = pd.Series([100.0, 100.0, 100.0])
    assert OBV(flat_close, flat_vol).iloc[-1] == 0


def test_vwap_为窗口内成交量加权均价(bars):
    v = VWAP(bars["high"], bars["low"], bars["close"], bars["volume"], period=5)
    # 成交量恒定时退化为典型价的简单均值
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3
    assert v.iloc[-1] == pytest.approx(typical.tail(5).mean())
    assert v.iloc[:4].isna().all()  # 窗口未满


def test_vwap_零成交量不除零(bars):
    v = VWAP(bars["high"], bars["low"], bars["close"], pd.Series([0.0] * len(bars)), period=5)
    assert v.isna().all()
    assert not np.isinf(v.fillna(0)).any()


def test_bbwidth_随波动扩张(bars):
    calm = pd.Series([10.0] * 30)
    assert BBWIDTH(calm, 20).iloc[-1] == pytest.approx(0.0)
    wide = pd.Series([10 + (i % 2) * 5 for i in range(30)], dtype=float)
    assert BBWIDTH(wide, 20).iloc[-1] > BBWIDTH(calm, 20).iloc[-1]


def test_新指标可用于选股表达式(bars):
    # 表达式白名单放行 + 求值可跑通（多列参数指标也能用）
    validate_expr("KDJ_K(high, low, close) > 50")
    assert isinstance(eval_expr_last("KDJ_K(high, low, close, 9) > 0", bars), bool)
    assert isinstance(eval_expr_last("OBV(close, volume) > 0", bars), bool)
    assert isinstance(eval_expr_last("close > VWAP(high, low, close, volume, 5)", bars), bool)
    assert isinstance(eval_expr_last("BBWIDTH(close, 5) < 1", bars), bool)


def test_日志筛选与导出(seeded):
    """信号列表筛选、CSV 导出、以及带时区偏移的时间参数归一化。"""
    import asyncio
    from datetime import datetime, timedelta, timezone

    from httpx import ASGITransport, AsyncClient

    from app.db.models import AuditLog, Signal

    base = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
    rows = [
        ("sig-a", "US.AAPL", "alpha", "tradingview", "executed", base),
        ("sig-b", "US.MSFT", "alpha", "strategy", "rejected_risk", base + timedelta(days=1)),
        ("sig-c", "HK.00700", "beta", "manual", "executed", base + timedelta(days=5)),
    ]
    for key, symbol, strategy, source, status, ts in rows:
        seeded.add(Signal(dedup_key=key, symbol=symbol, market=symbol.split(".")[0],
                          action="buy", strategy_name=strategy, source=source,
                          status=status, created_at=ts))
    seeded.add(AuditLog(username="admin", method="POST", path="/api/strategies",
                        status_code=200, ts=base))
    seeded.add(AuditLog(username="admin", method="DELETE", path="/api/notify/channels/1",
                        status_code=200, ts=base + timedelta(days=5)))
    seeded.commit()

    async def run():
        from app.main import create_app

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            login = await c.post("/api/auth/login",
                                 json={"username": "admin", "password": "admin123"})
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

            async def get(url, **params):
                return await c.get(url, params=params, headers=headers)

            # 按策略 / 来源 / 状态筛选
            assert (await get("/api/signals", strategy="alpha")).json()["total"] == 2
            assert (await get("/api/signals", source="manual")).json()["total"] == 1
            assert (await get("/api/signals", status="executed")).json()["total"] == 2
            # 标的模糊匹配
            assert (await get("/api/signals", symbol="AAPL")).json()["total"] == 1
            assert (await get("/api/signals", symbol="US.")).json()["total"] == 2
            # 组合条件取交集
            assert (await get("/api/signals", strategy="alpha", status="executed")).json()["total"] == 1

            # 日期区间（纯日期按 UTC 解释，末端补到当日 23:59:59）
            r = await get("/api/signals", date_from="2026-03-10", date_to="2026-03-11")
            assert r.json()["total"] == 2

            # 带偏移的时刻必须换算成 UTC。取一个恰好等于某条信号时刻的临界点：
            # 若偏移被直接丢弃（SQLAlchemy 的 SQLite 绑定就是这么做的），
            # +08:00 会被当成 20:00 墙钟值，12:00 那条就被漏掉 → 结果变成 1。
            utc_cut = "2026-03-11T12:00:00+00:00"
            plus8_cut = "2026-03-11T20:00:00+08:00"   # 同一时刻
            n_utc = (await get("/api/signals", date_from=utc_cut)).json()["total"]
            n_plus8 = (await get("/api/signals", date_from=plus8_cut)).json()["total"]
            assert n_utc == n_plus8 == 2

            # 非法时间参数明确报错而非静默忽略
            assert (await get("/api/signals", date_from="not-a-date")).status_code == 400

            # 导出：路由未被 /signals/{id} 吞掉，带 BOM，内容遵循筛选
            r = await get("/api/signals/export.csv", strategy="alpha")
            assert r.status_code == 200
            assert "text/csv" in r.headers["content-type"]
            assert "attachment" in r.headers["content-disposition"]
            text = r.content.decode("utf-8")
            assert text.startswith("﻿")           # Excel 识别 UTF-8
            assert "US.AAPL" in text and "HK.00700" not in text
            assert len(text.strip().splitlines()) == 3  # 表头 + 2 行

            # 详情路由仍正常（未被 export.csv 影响）
            sig_id = (await get("/api/signals")).json()["items"][0]["id"]
            assert (await get(f"/api/signals/{sig_id}")).status_code == 200

            # 审计日志筛选与导出
            assert (await get("/api/audit-logs", method="DELETE")).json()["total"] == 1
            assert (await get("/api/audit-logs", path="notify")).json()["total"] == 1
            assert (await get("/api/audit-logs", date_from="2026-03-14")).json()["total"] == 1
            r = await get("/api/audit-logs/export.csv", method="POST")
            assert r.status_code == 200
            assert "/api/strategies" in r.content.decode("utf-8")

    asyncio.run(run())


def test_新指标在策略命名空间可直接调用():
    from app.strategy.custom import compile_strategy_code

    code = '''class KdjStrategy(Strategy):
    params = {"qty": 100}

    def on_bar(self, ctx):
        bars = ctx.history(30)
        if len(bars) < 12:
            return
        k = KDJ_K(bars["high"], bars["low"], bars["close"])
        if k.iloc[-1] < 20 and ctx.position() == 0:
            ctx.buy(self.p["qty"])
        elif k.iloc[-1] > 80 and ctx.position() > 0:
            ctx.close()
'''
    cls = compile_strategy_code(code)
    assert cls.__name__ == "KdjStrategy"
