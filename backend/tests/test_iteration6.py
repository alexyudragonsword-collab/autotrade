"""迭代6：自定义策略编辑器与回测走查图。"""

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from app.strategy.custom import (
    DEFAULT_TEMPLATE,
    StrategyCodeError,
    compile_strategy_code,
    load_custom_strategy,
    validate_strategy_class,
)

GOOD_CODE = '''class AlwaysBuyOnce(Strategy):
    """测试：第一根 bar 买入。"""

    params = {"qty": 5}

    def on_bar(self, ctx):
        if ctx.position() == 0 and len(ctx.history(999)) == 1:
            ctx.buy(self.p["qty"])
'''

PORTFOLIO_CODE = '''class EqualWeight(PortfolioStrategy):
    """测试：等权持有全部标的。"""

    params = {"rebalance": "monthly"}

    def on_rebalance(self, ctx):
        target = ctx.equity() * 0.95 / len(ctx.symbols)
        for sym in ctx.symbols:
            ctx.order_target_value(sym, target)
'''


# ---------- 编译与校验 ----------


def test_compile_good_code():
    cls = compile_strategy_code(GOOD_CODE)
    assert cls.__name__ == "AlwaysBuyOnce"
    report = validate_strategy_class(cls)
    assert report["ok"] and report["kind"] == "single"
    assert report["trades"] >= 1


def test_compile_template():
    cls = compile_strategy_code(DEFAULT_TEMPLATE)
    assert validate_strategy_class(cls)["ok"]


def test_compile_portfolio_code():
    cls = compile_strategy_code(PORTFOLIO_CODE)
    report = validate_strategy_class(cls)
    assert report["kind"] == "portfolio"
    assert report["trades"] >= 2  # 两个合成标的都会建仓


def test_compile_rejects_bad_code():
    with pytest.raises(StrategyCodeError, match="执行失败"):
        compile_strategy_code("this is not python !!!")
    with pytest.raises(StrategyCodeError, match="必须定义"):
        compile_strategy_code("x = 1")
    with pytest.raises(StrategyCodeError, match="只允许定义一个"):
        compile_strategy_code(GOOD_CODE + "\n" + GOOD_CODE.replace("AlwaysBuyOnce", "Another"))


def test_validate_catches_runtime_error():
    broken = '''class Broken(Strategy):
    params = {}
    def on_bar(self, ctx):
        raise RuntimeError("boom")
'''
    cls = compile_strategy_code(broken)
    with pytest.raises(StrategyCodeError, match="试跑失败"):
        validate_strategy_class(cls)


def test_indicators_available_in_namespace():
    code = '''class UseIndicator(Strategy):
    params = {}
    def on_bar(self, ctx):
        bars = ctx.history(30)
        if len(bars) >= 20:
            _ = RSI(bars["close"], 14)
            _ = SMA(bars["close"], 20)
'''
    cls = compile_strategy_code(code)
    assert validate_strategy_class(cls)["ok"]


# ---------- 热加载与注册表 ----------


def test_custom_strategy_hot_reload(seeded):
    from app.db.models import CustomStrategy
    from app.strategy.registry import get_strategy_class, list_strategies

    seeded.add(CustomStrategy(class_name="AlwaysBuyOnce", code=GOOD_CODE))
    seeded.commit()

    cls = get_strategy_class("AlwaysBuyOnce")
    assert cls.__name__ == "AlwaysBuyOnce"
    assert cls.params == {"qty": 5}

    listed = {s["class_name"]: s for s in list_strategies()}
    assert listed["AlwaysBuyOnce"]["custom"] is True
    assert listed["SmaCross"]["custom"] is False

    # 修改代码 → updated_at 变化 → 重新编译
    row = seeded.scalar(select(CustomStrategy).where(CustomStrategy.class_name == "AlwaysBuyOnce"))
    row.code = GOOD_CODE.replace('"qty": 5', '"qty": 9')
    seeded.commit()
    assert get_strategy_class("AlwaysBuyOnce").params == {"qty": 9}

    # 停用后不可用
    row.enabled = False
    seeded.commit()
    assert load_custom_strategy("AlwaysBuyOnce") is None
    with pytest.raises(ValueError):
        get_strategy_class("AlwaysBuyOnce")


def test_custom_strategy_in_backtest_engine(seeded):
    from app.backtest.engine import BacktestEngine
    from app.db.models import CustomStrategy
    from app.strategy.registry import get_strategy_class

    seeded.add(CustomStrategy(class_name="AlwaysBuyOnce", code=GOOD_CODE))
    seeded.commit()
    cls = get_strategy_class("AlwaysBuyOnce")
    closes = [10.0, 11.0, 12.0]
    bars = {"X": pd.DataFrame({
        "open": closes, "high": closes, "low": closes, "close": closes, "volume": [1] * 3,
    }, index=pd.bdate_range("2026-01-01", periods=3))}
    result = BacktestEngine(cls, {}, bars, initial_cash=1000,
                            commission_bps=0, slippage_bps=0).run()
    assert len(result.trades) == 1
    assert result.trades[0].qty == 5


# ---------- API ----------


@pytest.fixture
async def client(seeded):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        c.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
        yield c


async def test_custom_strategy_api_crud(client, seeded):
    # 校验接口
    resp = await client.post("/api/custom-strategies/validate",
                             json={"class_name": "AlwaysBuyOnce", "code": GOOD_CODE})
    assert resp.status_code == 200
    assert resp.json()["detected_class"] == "AlwaysBuyOnce"

    # 保存：类名不匹配被拒
    resp = await client.post("/api/custom-strategies",
                             json={"class_name": "WrongName", "code": GOOD_CODE})
    assert resp.status_code == 400
    # 与内置同名被拒
    resp = await client.post("/api/custom-strategies",
                             json={"class_name": "SmaCross",
                                   "code": GOOD_CODE.replace("AlwaysBuyOnce", "SmaCross")})
    assert resp.status_code == 400
    # 正常保存
    resp = await client.post("/api/custom-strategies",
                             json={"class_name": "AlwaysBuyOnce", "code": GOOD_CODE})
    assert resp.status_code == 200
    sid = resp.json()["id"]

    # 出现在策略下拉
    resp = await client.get("/api/strategies/builtin")
    names = [s["class_name"] for s in resp.json()]
    assert "AlwaysBuyOnce" in names

    # 被策略配置引用时禁止删除
    from app.db.models import StrategyConfig

    seeded.add(StrategyConfig(name="uses_custom", class_name="AlwaysBuyOnce"))
    seeded.commit()
    resp = await client.delete(f"/api/custom-strategies/{sid}")
    assert resp.status_code == 400
    seeded.query(StrategyConfig).delete()
    seeded.commit()
    assert (await client.delete(f"/api/custom-strategies/{sid}")).status_code == 200


async def test_backtest_chart_endpoint(client, seeded, tmp_path, monkeypatch):
    import app.api.trading_config as tc_mod
    from app.data.store import BarStore
    from app.db.models import BacktestRun

    store = BarStore(base_dir=tmp_path)
    closes = list(np.linspace(10, 20, 30))
    store.save("US.CHT", pd.DataFrame({
        "open": closes, "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes], "close": closes, "volume": [100] * 30,
    }, index=pd.bdate_range("2026-01-01", periods=30)))
    monkeypatch.setattr("app.data.store._store", store)

    run = BacktestRun(strategy_class="SmaCross", params={}, symbols=["US.CHT"],
                      market="US", start_date="2026-01-01", end_date="2026-03-01",
                      status="done",
                      trades=[{"date": "2026-01-08", "symbol": "US.CHT", "side": "buy",
                               "qty": 10, "price": 12.0, "fee": 0.0, "pnl": None}])
    seeded.add(run)
    seeded.commit()

    resp = await client.get(f"/api/backtests/{run.id}/chart", params={"symbol": "US.CHT"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["kline"]) == 30
    assert len(data["kline"][0]) == 6  # [ts, open, close, low, high, volume]
    assert data["trades"][0]["side"] == "buy"

    resp = await client.get(f"/api/backtests/{run.id}/chart", params={"symbol": "US.OTHER"})
    assert resp.status_code == 400
