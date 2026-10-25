"""数据层：符号规范化、BarStore 增量缓存、akshare / yfinance 字段映射。

数据源一律 mock 成与真实返回同结构的数据（中文列名、yfinance 多级列与时区索引），不联网。
"""

import pandas as pd
import pytest

from app.data.base import (
    DataProvider,
    cn_symbol,
    empty_bars,
    normalize_list,
    normalize_symbol,
)
from app.domain.enums import Market


def _bars(closes, start="2026-01-01", freq="D"):
    n = len(closes)
    return pd.DataFrame({
        "open": closes, "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes], "close": closes, "volume": [1000.0] * n,
    }, index=pd.date_range(start, periods=n, freq=freq))


# ---------- 符号规范化 ----------


@pytest.mark.parametrize("exchange,ticker,expected", [
    ("NASDAQ", "aapl", (Market.US, "US.AAPL")),
    ("NYSE", "BRK.B", (Market.US, "US.BRK.B")),       # 股份类别的点不是市场前缀
    ("", "NASDAQ:TSLA", (Market.US, "US.TSLA")),       # 兼容 交易所:代码 写法
    ("HKEX", "700", (Market.HK, "HK.00700")),
    ("", "hk.700", (Market.HK, "HK.00700")),           # 规范格式透传并补零
    ("SSE", "600519", (Market.CN, "SH.600519")),
    ("SZSE", "1", (Market.CN, "SZ.000001")),
])
def test_normalize_symbol(exchange, ticker, expected):
    assert normalize_symbol(exchange, ticker) == expected


@pytest.mark.parametrize("exchange,ticker", [("MOEX", "GAZP"), ("", "AAPL"), ("HKEX", "ABC"), ("NASDAQ", "")])
def test_normalize_symbol_rejects(exchange, ticker):
    with pytest.raises(ValueError):
        normalize_symbol(exchange, ticker)


def test_cn_symbol_by_code_range():
    assert cn_symbol("600519") == "SH.600519"
    assert cn_symbol("688981") == "SH.688981"
    assert cn_symbol("300750") == "SZ.300750"
    assert cn_symbol("2") == "SZ.000002"
    with pytest.raises(ValueError):
        cn_symbol("830799")  # 北交所暂不支持


def test_normalize_list_bare_codes_per_market():
    assert normalize_list(Market.CN, "600519，000001, 600519") == ["SH.600519", "SZ.000001"]
    assert normalize_list(Market.US, ["aapl", "US.MSFT"]) == ["US.AAPL", "US.MSFT"]
    assert normalize_list(Market.HK, ["700", "HK.9988"]) == ["HK.00700", "HK.09988"]


# ---------- BarStore ----------


class RecordingProvider(DataProvider):
    """记录每次请求的区间，按预置的完整行情切片返回。"""

    def __init__(self, full: pd.DataFrame, fail: bool = False):
        self.full = full
        self.fail = fail
        self.calls: list[tuple[str, str, str]] = []

    def get_bars(self, symbol, start, end, interval="1d"):
        self.calls.append((start, end, interval))
        if self.fail:
            raise ConnectionError("数据源不可用")
        lo, hi = pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(days=1)
        return self.full[(self.full.index >= lo) & (self.full.index < hi)]

    def get_universe(self, market, universe):
        return []


@pytest.fixture
def make_store(tmp_path, monkeypatch):
    import app.data.store as store_mod
    from app.data.store import BarStore

    def _make(provider):
        monkeypatch.setattr(store_mod, "get_provider", lambda market: provider)
        return BarStore(base_dir=tmp_path)

    return _make


def test_store_empty_cache_fetches_and_persists(make_store, tmp_path):
    full = _bars([10.0 + i for i in range(30)])
    p = RecordingProvider(full)
    store = make_store(p)
    got = store.get_bars(Market.US, "US.AAA", "2026-01-05", "2026-01-10")
    assert len(got) == 6 and got["close"].iloc[0] == 14.0
    assert p.calls == [("2026-01-05", "2026-01-10", "1d")]
    assert (tmp_path / "US_AAA.parquet").exists()
    assert store.last_close("US.AAA") == 19.0


def test_store_accepts_bare_symbol_with_market(make_store):
    """回测页默认填裸代码 AAPL / 600519：按 market 规范化后命中 US_AAPL / SH_600519 缓存。"""
    store = make_store(RecordingProvider(empty_bars()))
    store.save("US.AAPL", _bars([10.0, 11.0]))
    store.save("SH.600519", _bars([1500.0]))
    assert len(store.get_bars(Market.US, "AAPL", "2026-01-01", "2026-01-31", refresh=False)) == 2
    assert len(store.get_bars(Market.CN, "600519", "2026-01-01", "2026-01-31", refresh=False)) == 1
    assert len(store.get_bars(Market.US, "aapl", "2026-01-01", "2026-01-31", refresh=False)) == 2


def test_store_refresh_throttled_and_refresh_false_offline(make_store):
    p = RecordingProvider(_bars([10.0] * 30))
    store = make_store(p)
    store.get_bars(Market.US, "US.AAA", "2026-01-01", "2026-01-10")
    n = len(p.calls)
    store.get_bars(Market.US, "US.AAA", "2026-01-01", "2026-01-10")
    assert len(p.calls) == n  # 同一缺口 10 分钟内不重复请求
    store.get_bars(Market.US, "US.AAA", "2026-01-01", "2026-01-20", refresh=False)
    assert len(p.calls) == n  # refresh=False 绝不联网


def test_store_recent_last_bar_is_refetched(make_store):
    """缓存末根是今天的 bar（可能盘中未收完）→ end 恰好等于它也要补拉；历史末根不重复拉。"""
    from datetime import date, timedelta

    today = date.today()
    recent = _bars([10.0] * 10, start=(today - timedelta(days=9)).isoformat())
    p = RecordingProvider(recent)
    store = make_store(p)
    store.save("US.AAA", recent)
    store.get_bars(Market.US, "US.AAA", recent.index[0].date().isoformat(), today.isoformat())
    assert p.calls and p.calls[-1][1] == today.isoformat()


def test_store_head_and_tail_gaps_with_overlap(make_store):
    full = _bars([10.0 + i for i in range(40)])          # 01-01 ~ 02-09
    p = RecordingProvider(full)
    store = make_store(p)
    store.save("US.AAA", full.loc["2026-01-10":"2026-01-20"])
    got = store.get_bars(None, "US.AAA", "2026-01-05", "2026-01-25")  # market=None 按前缀推断
    starts = sorted(c[0] for c in p.calls)
    assert starts[0] == "2026-01-05"                      # 头部缺口
    assert "2026-01-16" in starts                         # 尾部从缓存倒数第 5 根起重叠拉取
    assert got.index[0] == pd.Timestamp("2026-01-05") and got.index[-1] == pd.Timestamp("2026-01-25")
    assert not got.index.duplicated().any()
    assert len(store.load("US.AAA")) == 21                # 01-05 ~ 01-25 全部落盘


def test_store_source_failure_falls_back_to_cache(make_store):
    p = RecordingProvider(_bars([1.0]), fail=True)
    store = make_store(p)
    store.save("US.AAA", _bars([10.0, 11.0, 12.0]))
    got = store.get_bars(Market.US, "US.AAA", "2026-01-01", "2026-12-31")
    assert list(got["close"]) == [10.0, 11.0, 12.0]       # 不抛错，退回缓存
    assert p.calls                                         # 确实尝试过拉取


def test_store_detects_dividend_readjustment(make_store):
    """前复权：除权后历史价格整体下调，重叠段对不上 → 整段重拉替换旧缓存。"""
    old = _bars([100.0] * 20)
    store = make_store(RecordingProvider(old))
    store.save("SH.600000", old)
    readjusted = _bars([95.0] * 25)                       # 新口径下历史价全部变了
    p = RecordingProvider(readjusted)
    store = make_store(p)
    got = store.get_bars(Market.CN, "SH.600000", "2026-01-01", "2026-01-25")
    assert set(got["close"]) == {95.0}                    # 没有新旧口径混杂
    assert len(got) == 25


def test_store_unchanged_history_merges_incrementally(make_store):
    full = _bars([100.0 + i for i in range(25)])
    p = RecordingProvider(full)
    store = make_store(p)
    store.save("SH.600000", full.iloc[:20])
    got = store.get_bars(Market.CN, "SH.600000", "2026-01-01", "2026-01-25")
    assert len(got) == 25 and got["close"].is_monotonic_increasing
    assert all(c[0] != "2026-01-01" for c in p.calls)     # 口径一致时不整段重拉


def test_store_minute_end_day_inclusive(make_store):
    store = make_store(RecordingProvider(empty_bars()))
    store.save("US.X", _bars([1.0] * 10, start="2026-01-02 09:30", freq="15min"), interval="15m")
    got = store.get_bars(None, "US.X", "2026-01-02", "2026-01-02", refresh=False, interval="15m")
    assert len(got) == 10


def test_get_provider_routing(monkeypatch):
    monkeypatch.undo()  # 撤销 conftest 的离线替换，测真实路由（只构造对象、不联网）
    from app.data.akshare_provider import AkshareProvider
    from app.data.store import _PROVIDERS, get_provider
    from app.data.yfinance_provider import YFinanceProvider

    _PROVIDERS.clear()
    assert isinstance(get_provider(Market.CN), AkshareProvider)
    assert isinstance(get_provider(Market.HK), AkshareProvider)
    assert isinstance(get_provider(Market.US), YFinanceProvider)
    with pytest.raises(ValueError):
        get_provider(Market.CRYPTO)


# ---------- akshare 字段映射 ----------


def _ak_daily():
    return pd.DataFrame({
        "日期": ["2026-01-05", "2026-01-06"], "股票代码": ["600519", "600519"],
        "开盘": [1500.0, 1510.0], "收盘": [1505.0, 1520.0], "最高": [1512.0, 1525.0],
        "最低": [1495.0, 1508.0], "成交量": [12000, 13000], "成交额": [1.8e9, 2.0e9],
    })


def test_akshare_cn_daily(monkeypatch):
    import akshare

    from app.data.akshare_provider import AkshareProvider

    seen = {}

    def fake(**kw):
        seen.update(kw)
        return _ak_daily()

    monkeypatch.setattr(akshare, "stock_zh_a_hist", fake)
    df = AkshareProvider().get_bars("SH.600519", "2026-01-05", "2026-01-06")
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df["close"].tolist() == [1505.0, 1520.0]
    assert isinstance(df.index, pd.DatetimeIndex)
    assert seen == {"symbol": "600519", "period": "daily", "start_date": "20260105",
                    "end_date": "20260106", "adjust": "qfq"}


def test_akshare_hk_and_minute(monkeypatch):
    import akshare

    from app.data.akshare_provider import AkshareProvider

    monkeypatch.setattr(akshare, "stock_hk_hist", lambda **kw: _ak_daily())
    assert len(AkshareProvider().get_bars("HK.00700", "2026-01-05", "2026-01-06")) == 2

    minute = pd.DataFrame({"时间": ["2026-01-05 09:35:00", "2026-01-05 09:40:00"],
                           "开盘": [10.0, 10.1], "收盘": [10.1, 10.2], "最高": [10.2, 10.3],
                           "最低": [9.9, 10.0], "成交量": [500, 600]})
    seen = {}
    monkeypatch.setattr(akshare, "stock_zh_a_hist_min_em", lambda **kw: seen.update(kw) or minute)
    df = AkshareProvider().get_bars("SZ.000001", "2026-01-05", "2026-01-05", interval="5m")
    assert df.index[0] == pd.Timestamp("2026-01-05 09:35")
    assert seen["period"] == "5" and seen["symbol"] == "000001"

    with pytest.raises(ValueError, match="仅支持 A股"):
        AkshareProvider().get_bars("HK.00700", "2026-01-05", "2026-01-05", interval="5m")
    with pytest.raises(ValueError, match="不支持周期"):
        AkshareProvider().get_bars("SH.600519", "2026-01-05", "2026-01-05", interval="30m")


def test_akshare_index_universe(monkeypatch):
    import akshare

    from app.data.akshare_provider import AkshareProvider

    cons = pd.DataFrame({
        "成分券代码": ["600519", "1", "300750", "830799"],
        "交易所": ["上海证券交易所", "深圳证券交易所", "", ""],   # 后两行交易所缺失，按代码段判断
    })
    seen = {}
    monkeypatch.setattr(akshare, "index_stock_cons_csindex", lambda **kw: seen.update(kw) or cons)
    got = AkshareProvider().get_universe(Market.CN, {"type": "index", "value": "沪深300"})
    assert got == ["SH.600519", "SZ.000001", "SZ.300750"]  # 北交所代码跳过
    assert seen == {"symbol": "000300"}
    with pytest.raises(ValueError, match="未知指数"):
        AkshareProvider().get_universe(Market.CN, {"type": "index", "value": "纳斯达克"})
    with pytest.raises(ValueError):
        AkshareProvider().get_universe(Market.HK, {"type": "index", "value": "恒生指数"})


def test_akshare_fundamentals(monkeypatch):
    import akshare

    from app.data.akshare_provider import AkshareProvider

    spot = pd.DataFrame({
        "代码": ["600519", "000001", "830799"], "名称": ["贵州茅台", "平安银行", "北交所股"],
        "最新价": [1500.0, 11.2, 5.0], "市盈率-动态": [25.1, 4.8, "-"],
        "市净率": [8.2, 0.6, 1.0], "总市值": [1.9e12, 2.2e11, 1e9], "换手率": [0.3, 0.8, 2.0],
    })
    monkeypatch.setattr(akshare, "stock_zh_a_spot_em", lambda: spot)
    p = AkshareProvider()
    f = p.get_fundamentals(Market.CN, ["SH.600519", "SZ.000001", "SH.999999"])
    assert list(f.index) == ["SH.600519", "SZ.000001"]
    assert f.loc["SH.600519", "pe_ttm"] == 25.1 and f.loc["SZ.000001", "name"] == "平安银行"
    assert {"pb", "market_cap", "turnover_rate", "last_price"} <= set(f.columns)
    assert p.get_fundamentals(Market.HK, ["HK.00700"]).empty


# ---------- yfinance 字段映射 ----------


def _yf_frame(index):
    cols = pd.MultiIndex.from_product([["Close", "High", "Low", "Open", "Volume"], ["AAPL"]],
                                      names=["Price", "Ticker"])
    n = len(index)
    data = [[200.0 + i, 202.0 + i, 198.0 + i, 199.0 + i, 1e6] for i in range(n)]
    return pd.DataFrame(data, index=index, columns=cols)


def test_yfinance_daily_multiindex(monkeypatch):
    import yfinance

    from app.data.yfinance_provider import YFinanceProvider, to_yahoo

    seen = {}

    def fake(ticker, **kw):
        seen.update(kw, ticker=ticker)
        return _yf_frame(pd.DatetimeIndex(["2026-01-05", "2026-01-06"], name="Date"))

    monkeypatch.setattr(yfinance, "download", fake)
    df = YFinanceProvider().get_bars("US.AAPL", "2026-01-05", "2026-01-06")
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df["close"].tolist() == [200.0, 201.0]
    assert seen["end"] == "2026-01-07"          # yfinance 的 end 不含当日，需 +1 天
    assert seen["ticker"] == "AAPL" and seen["auto_adjust"] is True
    assert to_yahoo("US.BRK.B") == "BRK-B"


def test_yfinance_intraday_tz_and_window(monkeypatch):
    from datetime import date, timedelta

    import yfinance

    from app.data.yfinance_provider import YFinanceProvider

    seen = {}
    utc_idx = pd.DatetimeIndex(["2026-09-08 13:30", "2026-09-08 13:45"], tz="UTC", name="Datetime")
    monkeypatch.setattr(yfinance, "download", lambda t, **kw: seen.update(kw) or _yf_frame(utc_idx))
    df = YFinanceProvider().get_bars("US.AAPL", "2020-01-01", date.today().isoformat(), interval="15m")
    assert df.index[0] == pd.Timestamp("2026-09-08 09:30")  # 统一成美东本地时间、去时区
    assert df.index.tz is None
    assert date.fromisoformat(seen["start"]) >= date.today() - timedelta(days=60)  # 分钟线窗口被收窄
    with pytest.raises(ValueError, match="不支持周期"):
        YFinanceProvider().get_bars("US.AAPL", "2026-01-01", "2026-01-02", interval="1m")


def test_yfinance_universe():
    from app.data.yfinance_provider import YFinanceProvider

    p = YFinanceProvider()
    mega = p.get_universe(Market.US, {"type": "index", "value": "us_mega"})
    assert "US.AAPL" in mega and "US.BRK.B" in mega
    assert p.get_universe(Market.US, {"type": "symbols", "value": ["msft"]}) == ["US.MSFT"]
    with pytest.raises(ValueError):
        p.get_universe(Market.US, {"type": "index", "value": "沪深300"})
