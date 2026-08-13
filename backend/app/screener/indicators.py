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


INDICATOR_FUNCS = {
    "SMA": SMA, "EMA": EMA, "RSI": RSI, "MACD": MACD, "ATR": ATR,
    "HIGHEST": HIGHEST, "LOWEST": LOWEST, "STD": STD, "REF": REF,
    "ABS": lambda s: s.abs(),
}
