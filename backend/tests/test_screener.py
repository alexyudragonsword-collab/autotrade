import numpy as np
import pandas as pd
import pytest

from app.screener.expressions import ExprError, eval_expr_last, validate_expr


def _bars(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame({
        "open": closes, "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes], "close": closes,
        "volume": [1000] * n,
    }, index=pd.date_range("2026-01-01", periods=n))


def test_simple_comparison():
    bars = _bars(list(np.linspace(50, 100, 60)))
    assert eval_expr_last("close > 99", bars)
    assert not eval_expr_last("close < 50", bars)


def test_indicator_call():
    bars = _bars(list(np.linspace(50, 100, 60)))  # 单边上涨
    assert eval_expr_last("close > SMA(close, 20)", bars)
    assert eval_expr_last("RSI(close, 14) > 50", bars)


def test_bool_ops():
    bars = _bars(list(np.linspace(50, 100, 60)))
    assert eval_expr_last("close > 99 and volume > 500", bars)
    assert eval_expr_last("close < 50 or close > 99", bars)
    assert eval_expr_last("not (close < 50)", bars)


def test_arithmetic():
    bars = _bars([100.0] * 40)
    assert eval_expr_last("close * 2 > 199", bars)
    assert eval_expr_last("(high - low) / close < 0.05", bars)


def test_rejects_dangerous_expr():
    for bad in [
        "__import__('os').system('id')",
        "open('/etc/passwd')",
        "close.attr",
        "[1,2,3]",
        "'str' == 'str'",
        "lambda: 1",
        "foo(close)",
        "unknown_var > 1",
    ]:
        with pytest.raises(ExprError):
            validate_expr(bad)


def test_nan_result_is_false():
    bars = _bars([100.0] * 5)  # 数据不够算 SMA(20) → NaN
    assert eval_expr_last("close > SMA(close, 20)", bars) is False


def test_screener_engine_with_local_data(seeded, tmp_path, monkeypatch):
    """构造本地缓存数据跑通 ScreenerEngine 技术面过滤。"""
    from app.data.base import DataProvider
    from app.data.store import BarStore
    from app.db.models import ScreenerConfig
    from app.domain.enums import Market
    from app.screener import engine as engine_mod
    from app.screener.engine import ScreenerEngine

    store = BarStore(base_dir=tmp_path)
    # 上涨的票（RSI 高）与下跌的票（RSI 低）
    store.save("US.UP", _bars(list(np.linspace(50, 100, 60))))
    store.save("US.DOWN", _bars(list(np.linspace(100, 50, 60))))

    class FakeProvider(DataProvider):
        markets = {Market.US}

        def get_bars(self, symbol, start, end):
            return pd.DataFrame()

        def get_universe(self, market, universe):
            return ["US.UP", "US.DOWN"]

    monkeypatch.setattr(engine_mod, "get_provider", lambda m: FakeProvider())

    cfg = ScreenerConfig(name="rsi_low", market="US",
                         universe={"type": "symbols", "value": ["US.UP", "US.DOWN"]},
                         rules={"conditions": [{"expr": "RSI(close, 14) < 40"}]})
    seeded.add(cfg)
    seeded.commit()

    screener = ScreenerEngine()
    screener.store = store
    # 关闭增量刷新：FakeProvider 返回空
    result = screener.run(seeded, cfg.id)
    assert result.error_msg is None
    symbols = [r["symbol"] for r in result.symbols]
    assert symbols == ["US.DOWN"]
