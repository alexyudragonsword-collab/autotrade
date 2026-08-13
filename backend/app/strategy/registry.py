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
    if cls is not None:
        return cls
    from app.strategy.custom import load_custom_strategy

    custom = load_custom_strategy(name)
    if custom is not None:
        return custom
    raise ValueError(f"未知策略类: {name}，内置: {list(_REGISTRY)}（或检查自定义策略是否启用）")


def is_portfolio(cls: type) -> bool:
    return issubclass(cls, PortfolioStrategy)


def list_strategies() -> list[dict]:
    builtin = [
        {
            "class_name": name,
            "params": cls.params,
            "doc": (cls.__doc__ or "").strip().split("\n")[0],
            "kind": "portfolio" if issubclass(cls, PortfolioStrategy) else "single",
            "custom": False,
        }
        for name, cls in _REGISTRY.items()
    ]
    from app.strategy.custom import list_custom_strategies

    taken = {b["class_name"] for b in builtin}
    return builtin + [c for c in list_custom_strategies() if c["class_name"] not in taken]


__all__ = ["get_strategy_class", "is_portfolio", "list_strategies", "Strategy"]
