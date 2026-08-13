"""内置策略注册表：class_name → Strategy 子类。"""

from app.strategy.base import Strategy
from app.strategy.builtin.rsi_reversion import RsiReversion
from app.strategy.builtin.sma_cross import SmaCross

_REGISTRY: dict[str, type[Strategy]] = {
    "SmaCross": SmaCross,
    "RsiReversion": RsiReversion,
}


def get_strategy_class(name: str) -> type[Strategy]:
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"未知策略类: {name}，可用: {list(_REGISTRY)}")
    return cls


def list_strategies() -> list[dict]:
    return [
        {"class_name": name, "params": cls.params, "doc": (cls.__doc__ or "").strip()}
        for name, cls in _REGISTRY.items()
    ]
