"""K 线本地缓存：每个标的每个周期一个 parquet 文件，按需从数据源增量补齐。

- 文件：{base_dir}/US_AAPL.parquet（日线）、US_AAPL_15m.parquet（分钟线）
- get_bars 先读缓存，refresh=True 时只补缺口（区间头部 / 缓存末尾之后），
  拉取失败不抛错、退回缓存——离线或数据源抽风时回测和选股仍可用已有数据
- 日线是前复权数据：除权除息后历史价格会整体变化，检测到重叠段价格对不上时整段重拉
"""

import logging
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from app.config import get_settings
from app.data.akshare_provider import AkshareProvider
from app.data.base import DataProvider, empty_bars, market_of, normalize_list, tidy_bars
from app.data.yfinance_provider import YFinanceProvider
from app.domain.enums import Market

logger = logging.getLogger(__name__)

_PROVIDERS: dict[Market, DataProvider] = {}


def get_provider(market: Market) -> DataProvider:
    if not _PROVIDERS:
        ak, yf = AkshareProvider(), YFinanceProvider()
        _PROVIDERS.update({Market.CN: ak, Market.HK: ak, Market.US: yf})
    provider = _PROVIDERS.get(Market(market))
    if provider is None:
        raise ValueError(f"暂不支持该市场的行情数据: {market}")
    return provider


_REFRESH_TTL = 600          # 同一缺口 10 分钟内不重复请求（选股器扫全池时避免打爆数据源）
_OVERLAP_BARS = 5           # 增量拉取与缓存重叠的根数，用于发现复权口径变化
_ADJUST_TOLERANCE = 0.005   # 重叠段收盘价偏差超过 0.5% 视为复权因子已变化


class BarStore:
    def __init__(self, base_dir: str | Path | None = None):
        self.base_dir = Path(base_dir) if base_dir else get_settings().data_path / "bars"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._attempts: dict[tuple, float] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ---------- 文件读写 ----------

    def _path(self, symbol: str, interval: str = "1d") -> Path:
        stem = symbol.replace(".", "_").replace("/", "_")
        return self.base_dir / (f"{stem}.parquet" if interval == "1d" else f"{stem}_{interval}.parquet")

    def _lock(self, path: Path) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(str(path), threading.Lock())

    def load(self, symbol: str, interval: str = "1d") -> pd.DataFrame:
        path = self._path(symbol, interval)
        if not path.exists():
            return empty_bars()
        with self._lock(path):
            try:
                return tidy_bars(pd.read_parquet(path))
            except Exception:
                logger.warning("行情缓存损坏，忽略: %s", path)
                return empty_bars()

    def save(self, symbol: str, df: pd.DataFrame, interval: str = "1d") -> None:
        path = self._path(symbol, interval)
        data = tidy_bars(df)
        with self._lock(path):
            tmp = path.with_suffix(".tmp")
            data.to_parquet(tmp)
            tmp.replace(path)  # 原子替换，读者不会看到写了一半的文件

    # ---------- 查询 ----------

    def get_bars(self, market: Market | None, symbol: str, start: str, end: str,
                 refresh: bool = True, interval: str = "1d") -> pd.DataFrame:
        """[start, end] 区间（含两端日期，分钟线含 end 当天全部盘中 bar）的 K 线。

        symbol 可以是裸代码（回测页默认填 AAPL / 600519），按 market 规范化后再读缓存。
        """
        market = Market(market) if market else market_of(symbol)
        symbol = normalize_list(market, [symbol])[0]
        cached = self.load(symbol, interval)
        if refresh:
            cached = self._refresh(market, symbol, cached,
                                   str(start)[:10], str(end)[:10], interval)
        if cached.empty:
            return cached
        lo = pd.Timestamp(str(start)[:10])
        hi = pd.Timestamp(str(end)[:10]) + pd.Timedelta(days=1)
        return cached[(cached.index >= lo) & (cached.index < hi)]

    def last_close(self, symbol: str) -> float | None:
        """本地日线缓存的最新收盘价（只读缓存，不联网——供守护/模拟撮合做兜底价）。"""
        bars = self.load(symbol)
        if bars.empty:
            return None
        return float(bars["close"].iloc[-1])

    # ---------- 增量刷新 ----------

    def _due(self, key: tuple) -> bool:
        now = time.time()
        last = self._attempts.get(key)
        if last is not None and now - last < _REFRESH_TTL:
            return False
        self._attempts[key] = now
        return True

    def _refresh(self, market: Market, symbol: str, cached: pd.DataFrame,
                 start: str, end: str, interval: str) -> pd.DataFrame:
        gaps: list[tuple[str, str]] = []
        if cached.empty:
            gaps.append((start, end))
        else:
            first = cached.index[0].date().isoformat()
            last = cached.index[-1].date()
            if start < first:
                gaps.append((start, first))
            end_d = date.fromisoformat(end)
            # 缓存末根若是近期（今天/昨天，美股按北京时间跨日）可能是盘中未收完的 bar，
            # end 恰好等于它时也补拉；历史区间的末根是定稿数据，不必重复请求
            if end_d > last or (end_d == last and last >= date.today() - timedelta(days=1)):
                tail_from = cached.index[max(0, len(cached) - _OVERLAP_BARS)].date()
                gaps.append((tail_from.isoformat(), end))
        gaps = [g for g in gaps if g[0] <= g[1] and self._due((symbol, interval) + g)]
        if not gaps:
            return cached

        try:
            provider = get_provider(market)
        except ValueError as e:
            logger.warning("%s：%s", symbol, e)
            return cached

        merged = cached
        changed = False
        for g_start, g_end in gaps:
            try:
                fresh = tidy_bars(provider.get_bars(symbol, g_start, g_end, interval=interval))
            except Exception as e:  # 数据源失败不影响已有缓存
                logger.warning("拉取 %s %s [%s, %s] 失败，使用本地缓存: %s",
                               symbol, interval, g_start, g_end, e)
                continue
            if fresh.empty:
                continue
            if interval == "1d" and self._adjustment_changed(merged, fresh):
                logger.info("%s 复权口径变化（除权除息），整段重拉", symbol)
                try:
                    full_start = min(start, merged.index[0].date().isoformat())
                    fresh = tidy_bars(provider.get_bars(symbol, full_start, end, interval=interval))
                    merged = empty_bars()
                except Exception as e:
                    logger.warning("%s 整段重拉失败，保留旧缓存: %s", symbol, e)
                    continue
            merged = fresh if merged.empty else tidy_bars(pd.concat([merged, fresh]))
            changed = True

        if changed:
            self.save(symbol, merged, interval)
        return merged

    @staticmethod
    def _adjustment_changed(cached: pd.DataFrame, fresh: pd.DataFrame) -> bool:
        """比较重叠段（不含缓存最后一根——它可能是盘中未收完的 bar）的收盘价。"""
        if cached.empty:
            return False
        overlap = cached.index[:-1].intersection(fresh.index)
        if overlap.empty:
            return False
        old = cached.loc[overlap, "close"]
        new = fresh.loc[overlap, "close"]
        rel = ((new - old).abs() / old.abs().where(old != 0)).max()
        return bool(pd.notna(rel) and rel > _ADJUST_TOLERANCE)


_store: BarStore | None = None


def get_bar_store() -> BarStore:
    global _store
    if _store is None:
        _store = BarStore()
    return _store
