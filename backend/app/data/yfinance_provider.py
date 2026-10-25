"""美股数据源：yfinance（延迟安装，import 放函数内）。"""

import logging
import time
from datetime import date, timedelta

import pandas as pd

from app.data.base import DataProvider, empty_bars, normalize_list, tidy_bars
from app.domain.enums import Market

logger = logging.getLogger(__name__)

_INTERVALS = {"1d": "1d", "60m": "60m", "15m": "15m", "5m": "5m"}
_INTRADAY_DAYS = 59  # yfinance 分钟级只给近 60 天，越界请求会整体报错

# 选股器 universe 的内置美股大盘股池
US_MEGA = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "TSLA", "BRK.B", "JPM",
    "LLY", "V", "UNH", "XOM", "MA", "COST", "HD", "PG", "JNJ", "NFLX",
    "ORCL", "WMT", "ABBV", "BAC", "CRM",
]

_FUND_TTL = 6 * 3600


def to_yahoo(symbol: str) -> str:
    """US.BRK.B → BRK-B（Yahoo 用连字符表示股份类别）。"""
    code = symbol.split(".", 1)[1] if symbol.upper().startswith("US.") else symbol
    return code.replace(".", "-").upper()


class YFinanceProvider(DataProvider):
    markets = {Market.US}
    intervals = tuple(_INTERVALS)

    def __init__(self):
        self._fund_cache: dict[str, tuple[float, dict]] = {}

    def get_bars(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        import yfinance as yf

        yf_interval = _INTERVALS.get(interval)
        if yf_interval is None:
            raise ValueError(f"yfinance 不支持周期 {interval}")
        start_d = date.fromisoformat(str(start)[:10])
        end_d = date.fromisoformat(str(end)[:10])
        # yfinance 分钟级仅提供近 60 天（1m 仅 7 天，故最小支持 5m）
        if interval != "1d":
            start_d = max(start_d, date.today() - timedelta(days=_INTRADAY_DAYS))
        if start_d > end_d:
            return empty_bars()
        df = yf.download(to_yahoo(symbol), start=start_d.isoformat(),
                         end=(end_d + timedelta(days=1)).isoformat(),  # yfinance 的 end 不含当日
                         interval=yf_interval, auto_adjust=True, progress=False, threads=False)
        if df is None or df.empty:
            return empty_bars()
        if isinstance(df.columns, pd.MultiIndex):  # yfinance 新版本单票也返回 MultiIndex
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.lower)
        idx = pd.DatetimeIndex(df.index)
        if idx.tz is not None:  # 分钟线带时区：统一成美东本地时间（09:30 显示为 09:30）
            idx = idx.tz_convert("America/New_York").tz_localize(None)
        df.index = idx
        return tidy_bars(df)

    def get_universe(self, market: Market, universe: dict) -> list[str]:
        utype = (universe or {}).get("type")
        value = (universe or {}).get("value")
        if utype == "symbols":
            return normalize_list(Market.US, value)
        if utype == "index" and str(value or "").strip().upper() == "US_MEGA":
            return normalize_list(Market.US, US_MEGA)
        raise ValueError(f"yfinance universe 不支持: type={utype}, value={value}"
                         "（美股请用 US_MEGA 或自定义列表）")

    def get_fundamentals(self, market: Market, symbols: list[str]) -> pd.DataFrame:
        """逐票 Ticker.info 并发拉取 + 缓存；universe 大时较慢（建议 ≤50 只）。"""
        from concurrent.futures import ThreadPoolExecutor

        now = time.time()
        todo = [s for s in symbols
                if s not in self._fund_cache or now - self._fund_cache[s][0] > _FUND_TTL]
        if todo:
            with ThreadPoolExecutor(max_workers=8) as pool:
                for sym, info in zip(todo, pool.map(self._one_info, todo)):
                    if info:
                        self._fund_cache[sym] = (now, info)
        rows = {s: self._fund_cache[s][1] for s in symbols if s in self._fund_cache}
        return pd.DataFrame.from_dict(rows, orient="index")

    @staticmethod
    def _one_info(symbol: str) -> dict | None:
        try:
            import yfinance as yf

            info = yf.Ticker(to_yahoo(symbol)).info or {}
        except Exception:
            logger.warning("yfinance 基本面获取失败: %s", symbol)
            return None
        row = {
            "name": info.get("shortName") or info.get("longName"),
            "last_price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "pe_ttm": info.get("trailingPE"),
            "pb": info.get("priceToBook"),
            "market_cap": info.get("marketCap"),
        }
        return {k: v for k, v in row.items() if v is not None} or None
