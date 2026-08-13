"""期权合约模型。

规范符号格式：{前缀}.{代码}|{YYYYMMDD}|{C/P}|{行权价}
  例：US.AAPL|20250919|C|230 、 US.SPY|20251219|P|432.5 、 HK.00700|20250929|C|360

- 保留 US./HK. 前缀 → 现有按 symbol.split(".", 1)[0] 做市场路由的代码零改动
- 行权价用 %g 格式化（去掉多余的 0），到期日恒为 8 位数字
- 股票 symbol 不含 "|"，OptionContract.parse 对其返回 None
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.domain.enums import Market

_PREFIX_MARKET = {"US": Market.US, "HK": Market.HK, "SH": Market.CN, "SZ": Market.CN}

# 各市场期权到期按交易所本地日历计算剩余天数
_MARKET_TZ = {Market.US: "America/New_York", Market.HK: "Asia/Hong_Kong",
              Market.CN: "Asia/Shanghai"}


@dataclass(frozen=True)
class OptionContract:
    underlying: str  # 内部正股符号，如 US.AAPL / HK.00700
    expiry: str  # YYYYMMDD
    right: str  # "C" | "P"
    strike: float

    def symbol(self) -> str:
        return f"{self.underlying}|{self.expiry}|{self.right}|{self.strike:g}"

    @classmethod
    def parse(cls, symbol: str) -> "OptionContract | None":
        if "|" not in symbol:
            return None
        parts = symbol.split("|")
        if len(parts) != 4:
            return None
        underlying, expiry, right, strike_s = parts
        if not re.fullmatch(r"\d{8}", expiry) or right not in ("C", "P"):
            return None
        try:
            strike = float(strike_s)
        except ValueError:
            return None
        if strike <= 0 or "." not in underlying:
            return None
        return cls(underlying=underlying, expiry=expiry, right=right, strike=strike)

    @property
    def market(self) -> Market:
        prefix = self.underlying.split(".", 1)[0]
        market = _PREFIX_MARKET.get(prefix)
        if market is None:
            raise ValueError(f"无法识别期权标的市场: {self.underlying}")
        return market

    @property
    def ticker(self) -> str:
        return self.underlying.split(".", 1)[1]

    def display(self) -> str:
        """人类友好显示：AAPL 250919 C230"""
        return f"{self.ticker} {self.expiry[2:]} {self.right}{self.strike:g}"


def is_option(symbol: str) -> bool:
    return OptionContract.parse(symbol) is not None


def sec_type(symbol: str) -> str:
    return "option" if is_option(symbol) else "stock"


def days_to_expiry(symbol: str, now: datetime | None = None) -> int | None:
    """按交易所本地日历的自然日差；非期权返回 None。"""
    contract = OptionContract.parse(symbol)
    if contract is None:
        return None
    expiry_date = date(int(contract.expiry[:4]), int(contract.expiry[4:6]),
                       int(contract.expiry[6:8]))
    from datetime import timezone

    now = now or datetime.now(timezone.utc)
    tz = _MARKET_TZ.get(contract.market, "UTC")
    today = now.astimezone(ZoneInfo(tz)).date()
    return (expiry_date - today).days


def default_multiplier(symbol: str) -> float:
    """合约乘数兜底值（券商未报告时用）。股票为 1。"""
    contract = OptionContract.parse(symbol)
    if contract is None:
        return 1.0
    if contract.market == Market.US:
        return 100.0
    from app.config import get_settings

    return get_settings().hk_option_multiplier_default


def underlying_of(symbol: str) -> str:
    """期权返回标的正股符号；股票原样返回。"""
    contract = OptionContract.parse(symbol)
    return contract.underlying if contract else symbol


# ---------- 富途美股期权代码映射（HK 期权代码不可构造，走链查询缓存）----------

_FUTU_US_OPT_RE = re.compile(r"^US\.([A-Z.\-]+?)(\d{6})([CP])(\d+)$")


def to_futu_us_option_code(contract: OptionContract) -> str:
    """US.AAPL|20250919|C|230 → US.AAPL250919C230000（行权价×1000）"""
    strike_int = round(contract.strike * 1000)
    return f"US.{contract.ticker}{contract.expiry[2:]}{contract.right}{strike_int}"


def parse_futu_us_option_code(code: str) -> OptionContract | None:
    m = _FUTU_US_OPT_RE.match(code)
    if m is None:
        return None
    ticker, yymmdd, right, strike_raw = m.groups()
    year = int(yymmdd[:2])
    expiry = f"20{yymmdd}" if year < 70 else f"19{yymmdd}"
    return OptionContract(underlying=f"US.{ticker}", expiry=expiry, right=right,
                          strike=int(strike_raw) / 1000)
