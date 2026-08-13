"""BrokerManager：注册启用的适配器、健康检查、按市场路由、优雅降级。"""

import logging

from app.brokers.base import BrokerAdapter, BrokerError
from app.config import get_settings
from app.domain.enums import Market

logger = logging.getLogger(__name__)


class BrokerManager:
    def __init__(self):
        self._adapters: dict[str, BrokerAdapter] = {}
        self._last_error: dict[str, str] = {}

    def register(self, adapter: BrokerAdapter) -> None:
        self._adapters[adapter.name] = adapter

    async def connect_all(self) -> None:
        """逐个连接；单个失败不阻塞启动（优雅降级）。"""
        for name, adapter in self._adapters.items():
            try:
                await adapter.connect()
                self._last_error.pop(name, None)
                logger.info("券商 %s 已连接", name)
            except Exception as e:
                self._last_error[name] = str(e)
                logger.error("券商 %s 连接失败（系统继续运行）: %s", name, e)

    async def disconnect_all(self) -> None:
        for adapter in self._adapters.values():
            try:
                await adapter.disconnect()
            except Exception:
                logger.exception("断开 %s 失败", adapter.name)

    def get(self, name: str) -> BrokerAdapter:
        adapter = self._adapters.get(name)
        if adapter is None:
            raise BrokerError(f"券商 {name} 未启用")
        if not adapter.is_connected():
            err = self._last_error.get(name, "未连接")
            raise BrokerError(f"券商 {name} 离线: {err}")
        return adapter

    def get_if_connected(self, name: str) -> BrokerAdapter | None:
        adapter = self._adapters.get(name)
        return adapter if adapter and adapter.is_connected() else None

    def route(self, market: Market) -> BrokerAdapter:
        """按市场挑选一个在线且支持该市场的真实券商（paper 兜底除外）。"""
        for adapter in self._adapters.values():
            if adapter.name != "paper" and market in adapter.markets and adapter.is_connected():
                return adapter
        raise BrokerError(f"没有在线券商支持市场 {market}")

    async def reconnect(self, name: str) -> None:
        adapter = self._adapters.get(name)
        if adapter is None:
            raise BrokerError(f"券商 {name} 未启用")
        try:
            await adapter.disconnect()
        except Exception:
            pass
        await adapter.connect()
        self._last_error.pop(name, None)

    async def health_check(self) -> dict[str, dict]:
        """调度器周期调用；返回各 broker 状态并尝试自动重连。"""
        status = {}
        for name, adapter in self._adapters.items():
            ok = adapter.is_connected()
            if not ok:
                try:
                    await adapter.connect()
                    ok = adapter.is_connected()
                    if ok:
                        self._last_error.pop(name, None)
                        logger.info("券商 %s 自动重连成功", name)
                except Exception as e:
                    self._last_error[name] = str(e)
            status[name] = {"connected": ok, "error": self._last_error.get(name)}
        return status

    def status(self) -> dict[str, dict]:
        return {
            name: {"connected": a.is_connected(), "markets": sorted(a.markets),
                   "error": self._last_error.get(name)}
            for name, a in self._adapters.items()
        }


_manager: BrokerManager | None = None


def get_broker_manager() -> BrokerManager:
    global _manager
    if _manager is None:
        _manager = BrokerManager()
        _build_from_settings(_manager)
    return _manager


def _build_from_settings(manager: BrokerManager) -> None:
    settings = get_settings()
    if settings.paper_enabled:
        from app.brokers.paper import PaperBroker

        manager.register(PaperBroker(quote_fn=_live_quote_fn(manager)))
    if settings.futu_enabled:
        from app.brokers.futu_adapter import FutuAdapter

        manager.register(FutuAdapter())
    if settings.ibkr_enabled:
        from app.brokers.ibkr_adapter import IbkrAdapter

        manager.register(IbkrAdapter())


def _live_quote_fn(manager: BrokerManager):
    """Paper 撮合价优先用真实券商行情。"""

    async def quote(symbol: str) -> float | None:
        prefix = symbol.split(".", 1)[0]
        market = {"US": Market.US, "HK": Market.HK, "SH": Market.CN, "SZ": Market.CN}.get(prefix)
        if market is None:
            return None
        for adapter in manager._adapters.values():
            if adapter.name == "paper" or not adapter.is_connected() or market not in adapter.markets:
                continue
            q = await adapter.get_quote(symbol)
            if q is not None:
                return q.price
        return None

    return quote
