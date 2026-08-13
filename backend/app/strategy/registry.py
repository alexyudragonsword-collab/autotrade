"""内置策略注册表：class_name → 策略类（单标的 Strategy 或组合 PortfolioStrategy）。"""

from app.strategy.base import Strategy
from app.strategy.builtin.rsi_reversion import RsiReversion
from app.strategy.builtin.sma_cross import SmaCross
from app.strategy.portfolio import MomentumRotation, PortfolioStrategy

_REGISTRY: dict[str, type] = {
    "SmaCross": SmaCross,
    "RsiReversion": RsiReversion,
    "MomentumRotation": MomentumRotation,
}


def get_strategy_class(name: str) -> type:
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"未知策略类: {name}，可用: {list(_REGISTRY)}")
    return cls


def is_portfolio(cls: type) -> bool:
    return issubclass(cls, PortfolioStrategy)


def list_strategies() -> list[dict]:
    return [
        {
            "class_name": name,
            "params": cls.params,
            "doc": (cls.__doc__ or "").strip().split("\n")[0],
            "kind": "portfolio" if issubclass(cls, PortfolioStrategy) else "single",
        }
        for name, cls in _REGISTRY.items()
    ]


__all__ = ["get_strategy_class", "is_portfolio", "list_strategies", "Strategy"]
