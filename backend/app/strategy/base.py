"""策略基类 —— 回测与实盘共享同一份 on_bar 代码。"""

from abc import ABC, abstractmethod

import pandas as pd


class StrategyContext(ABC):
    """策略运行上下文。BacktestContext / LiveContext 各自实现。"""

    symbol: str = ""

    @abstractmethod
    def history(self, n: int) -> pd.DataFrame:
        """当前时点（含）往前 n 根日线，columns: open/high/low/close/volume。"""

    @abstractmethod
    def position(self) -> float:
        """当前持仓数量（0 = 空仓）。"""

    @abstractmethod
    def buy(self, qty: float, limit: float | None = None) -> None: ...

    @abstractmethod
    def sell(self, qty: float, limit: float | None = None) -> None: ...

    def close(self) -> None:
        pos = self.position()
        if pos > 0:
            self.sell(pos)

    def log(self, msg: str) -> None:  # noqa: B027
        pass


class Strategy(ABC):
    """继承并实现 on_bar；params 声明可调参数及默认值。

    同一份策略类既用于回测（BacktestEngine 驱动），也可注册为实盘策略
    （TV 告警里 strategy 字段对应 StrategyConfig.name，params 从配置覆盖）。
    """

    params: dict = {}

    def __init__(self, **overrides):
        self.p = {**self.params, **overrides}

    def on_start(self, ctx: StrategyContext) -> None:  # noqa: B027
        pass

    @abstractmethod
    def on_bar(self, ctx: StrategyContext) -> None: ...

    def on_stop(self, ctx: StrategyContext) -> None:  # noqa: B027
        pass
