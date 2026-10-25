"""A股/港股数据源：akshare（免费，接口偶有变动，全部调用带兜底）。"""

import logging
import time

import pandas as pd

from app.data.base import DataProvider, cn_symbol, empty_bars, normalize_list, tidy_bars
from app.domain.enums import Market

logger = logging.getLogger(__name__)

# 选股器 universe 支持的指数（也可直接填中证指数代码，如 000300）
INDEX_CODES = {
    "沪深300": "000300", "中证500": "000905", "上证50": "000016",
    "中证800": "000906", "中证1000": "000852", "科创50": "000688",
}
_MINUTE_PERIODS = {"60m": "60", "15m": "15", "5m": "5"}
_COLS = {"日期": "date", "时间": "date", "开盘": "open", "收盘": "close",
         "最高": "high", "最低": "low", "成交量": "volume"}
_SPOT_TTL = 600


def _ymd(d: str) -> str:
    return str(d)[:10].replace("-", "")


def _to_bars(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return empty_bars()
    df = df.rename(columns=_COLS)
    df = df.set_index(pd.to_datetime(df["date"]))
    return tidy_bars(df)


class AkshareProvider(DataProvider):
    markets = {Market.CN, Market.HK}
    intervals = ("1d", "60m", "15m", "5m")

    def __init__(self):
        self._spot: tuple[float, pd.DataFrame] | None = None

    def get_bars(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        prefix, _, code = symbol.upper().partition(".")
        if interval != "1d":
            if prefix not in ("SH", "SZ"):
                raise ValueError(f"akshare 分钟线仅支持 A股，{symbol} 不可用")
            return self._cn_minute(code, start, end, interval)
        if prefix in ("SH", "SZ"):
            fetch = "stock_zh_a_hist"
        elif prefix == "HK":
            fetch = "stock_hk_hist"
        else:
            raise ValueError(f"akshare 不支持的 symbol: {symbol}")

        import akshare as ak

        # 前复权：回测与实盘看到的价格连续；除权除息后由 BarStore 检测并整段重拉
        df = getattr(ak, fetch)(symbol=code, period="daily", start_date=_ymd(start),
                                end_date=_ymd(end), adjust="qfq")
        return _to_bars(df)

    @staticmethod
    def _cn_minute(code: str, start: str, end: str, interval: str) -> pd.DataFrame:
        period = _MINUTE_PERIODS.get(interval)
        if period is None:
            raise ValueError(f"akshare 不支持周期 {interval}")

        import akshare as ak

        # 分钟线不复权（数据源只有近期，复权口径意义不大，且避免缓存与新数据口径打架）
        df = ak.stock_zh_a_hist_min_em(symbol=code, start_date=f"{str(start)[:10]} 09:00:00",
                                       end_date=f"{str(end)[:10]} 15:30:00", period=period,
                                       adjust="")
        return _to_bars(df)

    def get_universe(self, market: Market, universe: dict) -> list[str]:
        utype = (universe or {}).get("type")
        value = (universe or {}).get("value")
        if utype == "symbols":
            return normalize_list(market, value)
        if utype == "index" and market == Market.CN:
            return self._index_members(str(value or "").strip())
        raise ValueError(f"akshare universe 不支持: market={market}, type={utype}")

    @staticmethod
    def _index_members(name: str) -> list[str]:
        code = INDEX_CODES.get(name, name)
        if not (code.isdigit() and len(code) == 6):
            raise ValueError(f"未知指数: {name}（支持 {'/'.join(INDEX_CODES)} 或 6 位中证指数代码）")

        import akshare as ak

        df = ak.index_stock_cons_csindex(symbol=code)
        out: list[str] = []
        for _, r in df.iterrows():
            raw = str(r["成分券代码"]).strip().zfill(6)
            exch = str(r.get("交易所", ""))
            try:
                if "上海" in exch:
                    sym = f"SH.{raw}"
                elif "深圳" in exch:
                    sym = f"SZ.{raw}"
                else:
                    sym = cn_symbol(raw)  # 交易所字段缺失时按代码段判断
            except ValueError:
                continue  # 北交所等暂不支持的代码直接跳过
            if sym not in out:
                out.append(sym)
        return out

    def get_fundamentals(self, market: Market, symbols: list[str]) -> pd.DataFrame:
        """A股：东方财富全市场快照（一次请求、10 分钟缓存）；港股暂无。

        注意 pe_ttm 取的是快照里的「市盈率-动态」列。
        """
        if market != Market.CN:
            return pd.DataFrame()
        spot = self._spot_snapshot()
        if spot.empty:
            return spot
        return spot.loc[[s for s in symbols if s in spot.index]]

    def _spot_snapshot(self) -> pd.DataFrame:
        if self._spot and time.time() - self._spot[0] < _SPOT_TTL:
            return self._spot[1]

        import akshare as ak

        df = ak.stock_zh_a_spot_em()
        rows = {}
        for _, r in df.iterrows():
            try:
                sym = cn_symbol(str(r["代码"]))
            except ValueError:
                continue
            rows[sym] = {
                "name": str(r["名称"]),
                "last_price": pd.to_numeric(r["最新价"], errors="coerce"),
                "pe_ttm": pd.to_numeric(r["市盈率-动态"], errors="coerce"),
                "pb": pd.to_numeric(r["市净率"], errors="coerce"),
                "market_cap": pd.to_numeric(r["总市值"], errors="coerce"),
                "turnover_rate": pd.to_numeric(r["换手率"], errors="coerce"),
            }
        snap = pd.DataFrame.from_dict(rows, orient="index")
        self._spot = (time.time(), snap)
        return snap
