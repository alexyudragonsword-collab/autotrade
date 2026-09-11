"""技术指标（纯 pandas 实现）。

所有函数接收/返回 pd.Series，可在选股表达式与策略中复用。
注册进 INDICATOR_FUNCS 的名字即选股表达式中允许调用的白名单函数。
"""

import pandas as pd


def SMA(s: pd.Series, period: int = 20) -> pd.Series:
    return s.rolling(int(period)).mean()


def EMA(s: pd.Series, period: int = 20) -> pd.Series:
    return s.ewm(span=int(period), adjust=False).mean()


def RSI(s: pd.Series, period: int = 14) -> pd.Series:
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rsi = 100 * gain / (gain + loss)
    return rsi.fillna(50.0)  # gain=loss=0（横盘）时取中性值 50


def MACD(s: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    """返回 MACD 柱（DIF-DEA）。"""
    dif = EMA(s, fast) - EMA(s, slow)
    dea = dif.ewm(span=int(signal), adjust=False).mean()
    return dif - dea


def ATR(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def HIGHEST(s: pd.Series, period: int = 20) -> pd.Series:
    return s.rolling(int(period)).max()


def LOWEST(s: pd.Series, period: int = 20) -> pd.Series:
    return s.rolling(int(period)).min()


def STD(s: pd.Series, period: int = 20) -> pd.Series:
    return s.rolling(int(period)).std()


def REF(s: pd.Series, n: int = 1) -> pd.Series:
    """N 根 bar 之前的值。"""
    return s.shift(int(n))


def BBWIDTH(s: pd.Series, period: int = 20, k: float = 2.0) -> pd.Series:
    """布林带宽 =（上轨 - 下轨）/ 中轨，即 2k×标准差 / 均线。

    量纲为比值（0.1 = 带宽为均线的 10%）。收窄至历史低位常预示变盘。
    """
    mid = SMA(s, period)
    return (2 * float(k) * STD(s, period)) / mid


def OBV(close: pd.Series, volume: pd.Series) -> pd.Series:
    """能量潮：收涨累加成交量、收跌累减，平盘不计。"""
    direction = close.diff().apply(lambda x: 0.0 if pd.isna(x) or x == 0 else (1.0 if x > 0 else -1.0))
    return (direction * volume).cumsum()


def VWAP(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series,
         period: int = 20) -> pd.Series:
    """滚动 N 根 bar 的成交量加权均价（典型价 = (H+L+C)/3）。

    注意：这是**滚动窗口**口径，不是交易所盘中那条从开盘累计的 VWAP——
    日线上做累计 VWAP 没有意义，故以 period 窗口滚动。
    """
    n = int(period)
    typical = (high + low + close) / 3
    vol_sum = volume.rolling(n).sum()
    return (typical * volume).rolling(n).sum() / vol_sum.where(vol_sum != 0)


def _kdj(high: pd.Series, low: pd.Series, close: pd.Series,
         period: int, k_period: int, d_period: int) -> tuple[pd.Series, pd.Series, pd.Series]:
    """KDJ 三线。K/D 用国内惯例的 SMA(X, N, 1) 平滑，等价于 alpha=1/N 的 EWM。"""
    n = int(period)
    hh, ll = HIGHEST(high, n), LOWEST(low, n)
    span = hh - ll
    # 区间为零（完全横盘）时 RSV 取中性值 50，避免除零
    rsv = ((close - ll) / span.where(span != 0) * 100).fillna(50.0)
    k = rsv.ewm(alpha=1 / int(k_period), adjust=False).mean()
    d = k.ewm(alpha=1 / int(d_period), adjust=False).mean()
    return k, d, 3 * k - 2 * d


def KDJ_K(high: pd.Series, low: pd.Series, close: pd.Series,
          period: int = 9, k_period: int = 3, d_period: int = 3) -> pd.Series:
    """KDJ 的 K 线（快线）。"""
    return _kdj(high, low, close, period, k_period, d_period)[0]


def KDJ_D(high: pd.Series, low: pd.Series, close: pd.Series,
          period: int = 9, k_period: int = 3, d_period: int = 3) -> pd.Series:
    """KDJ 的 D 线（慢线）。"""
    return _kdj(high, low, close, period, k_period, d_period)[1]


def KDJ_J(high: pd.Series, low: pd.Series, close: pd.Series,
          period: int = 9, k_period: int = 3, d_period: int = 3) -> pd.Series:
    """KDJ 的 J 线（3K−2D，最敏感，可超出 0~100）。"""
    return _kdj(high, low, close, period, k_period, d_period)[2]


INDICATOR_FUNCS = {
    "SMA": SMA, "EMA": EMA, "RSI": RSI, "MACD": MACD, "ATR": ATR,
    "HIGHEST": HIGHEST, "LOWEST": LOWEST, "STD": STD, "REF": REF,
    "BBWIDTH": BBWIDTH, "OBV": OBV, "VWAP": VWAP,
    "KDJ_K": KDJ_K, "KDJ_D": KDJ_D, "KDJ_J": KDJ_J,
    "ABS": lambda s: s.abs(),
}
