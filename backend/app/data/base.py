"""数据源抽象与符号规范化。

全系统统一使用带市场前缀的规范符号：US.AAPL / HK.00700 / SH.600519 / SZ.000001。
"""

from abc import ABC, abstractmethod

import pandas as pd

from app.domain.enums import Market

OHLCV = ["open", "high", "low", "close", "volume"]

_PREFIX_MARKET = {"US": Market.US, "HK": Market.HK, "SH": Market.CN, "SZ": Market.CN}

# TradingView 的 {{exchange}} 取值 → 市场
_US_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "ARCA", "NYSEARCA", "NYSE ARCA", "BATS", "CBOE", "OTC"}
_HK_EXCHANGES = {"HKEX", "HKG", "SEHK"}
_SH_EXCHANGES = {"SSE", "SH", "SHSE"}
_SZ_EXCHANGES = {"SZSE", "SZ"}


def market_of(symbol: str) -> Market:
    """规范符号 → 市场。"""
    prefix = symbol.split(".", 1)[0].upper()
    if prefix not in _PREFIX_MARKET:
        raise ValueError(f"无法识别市场前缀: {symbol}")
    return _PREFIX_MARKET[prefix]


def cn_symbol(code: str) -> str:
    """A股 6 位代码 → SH./SZ. 规范符号（6/9 开头沪市，0/2/3 开头深市）。"""
    code = str(code).strip().zfill(6)
    if not code.isdigit() or len(code) != 6:
        raise ValueError(f"A股代码应为 6 位数字: {code}")
    if code[0] in "69":
        return f"SH.{code}"
    if code[0] in "023":
        return f"SZ.{code}"
    raise ValueError(f"暂不支持的 A股代码（北交所等）: {code}")


def normalize_symbol(exchange: str, ticker: str) -> tuple[Market, str]:
    """TradingView 的 exchange + ticker → (市场, 规范符号)。无法识别时抛 ValueError。

    已是规范格式（US.TSLA）的直接透传；兼容 "NASDAQ:AAPL" 写法。
    """
    ticker = (ticker or "").strip().upper()
    exchange = (exchange or "").strip().upper()
    if ":" in ticker:
        ex, ticker = ticker.split(":", 1)
        exchange = exchange or ex
    if not ticker:
        raise ValueError("symbol 为空")

    prefix, _, rest = ticker.partition(".")
    if rest and prefix in _PREFIX_MARKET:
        market = _PREFIX_MARKET[prefix]
        if market == Market.HK:
            rest = rest.zfill(5)
        return market, f"{prefix}.{rest}"

    if exchange in _US_EXCHANGES:
        return Market.US, f"US.{ticker}"
    if exchange in _HK_EXCHANGES:
        if not ticker.isdigit():
            raise ValueError(f"港股代码应为数字: {ticker}")
        return Market.HK, f"HK.{ticker.zfill(5)}"
    if exchange in _SH_EXCHANGES:
        return Market.CN, f"SH.{ticker.zfill(6)}"
    if exchange in _SZ_EXCHANGES:
        return Market.CN, f"SZ.{ticker.zfill(6)}"
    raise ValueError(f"无法识别的交易所: {exchange or '(空)'}（symbol={ticker}）")


def normalize_list(market: Market, values) -> list[str]:
    """自定义标的池（用户可能只填裸代码 600519 / AAPL / 700）→ 规范符号列表，保序去重。"""
    if isinstance(values, str):
        values = [v for v in values.replace("，", ",").split(",")]
    out: list[str] = []
    for raw in values or []:
        v = str(raw).strip().upper()
        if not v:
            continue
        prefix = v.split(".", 1)[0]
        if "." in v and prefix in _PREFIX_MARKET:
            sym = normalize_symbol("", v)[1]
        elif market == Market.US:
            sym = f"US.{v}"
        elif market == Market.HK:
            sym = f"HK.{v.zfill(5)}"
        elif market == Market.CN:
            sym = cn_symbol(v)
        else:
            raise ValueError(f"暂不支持的市场: {market}")
        if sym not in out:
            out.append(sym)
    return out


def empty_bars() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=float) for c in OHLCV},
                        index=pd.DatetimeIndex([], name="date"))


def tidy_bars(df: pd.DataFrame) -> pd.DataFrame:
    """统一成：DatetimeIndex（升序、去重、无时区）+ float 型 OHLCV 五列。"""
    if df is None or df.empty:
        return empty_bars()
    missing = [c for c in OHLCV if c not in df.columns]
    if missing:
        raise ValueError(f"行情缺少列: {missing}")
    out = df[OHLCV].astype(float)
    idx = pd.DatetimeIndex(pd.to_datetime(out.index))
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    out.index = idx.rename("date")
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out.dropna(subset=["close"])


class DataProvider(ABC):
    """行情数据源。实现 get_bars / get_universe，基本面可选。"""

    markets: set[Market] = set()
    # 支持的 K 线周期；分钟级数据源覆盖范围有限（yfinance 60 天内，akshare 近期）
    intervals: tuple[str, ...] = ("1d",)

    @abstractmethod
    def get_bars(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        """[start, end]（含两端日期）的 K 线；index 为时间，列为 OHLCV；无数据返回空表。"""

    @abstractmethod
    def get_universe(self, market: Market, universe: dict) -> list[str]:
        """选股标的池：{"type": "index", "value": "沪深300"} 或 {"type": "symbols", "value": [...]}"""

    def get_fundamentals(self, market: Market, symbols: list[str]) -> pd.DataFrame:
        """基本面快照（index=规范符号）；默认空实现，表示该市场不支持基本面条件。"""
        return pd.DataFrame()
