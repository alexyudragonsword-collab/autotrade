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

    async def add_account(self, account_type: str, name: str, params: dict) -> BrokerAdapter:
        """运行时新增账户：注册并尝试连接（连接失败不抛出，交给健康检查重试）。"""
        if name in self._adapters:
            raise BrokerError(f"账户名 {name} 已存在")
        adapter = build_adapter(self, account_type, name, params)
        self.register(adapter)
        try:
            await adapter.connect()
        except Exception as e:
            self._last_error[name] = str(e)
            logger.warning("账户 %s 首次连接失败（健康检查将重试）: %s", name, e)
        return adapter

    async def remove_account(self, name: str) -> None:
        adapter = self._adapters.pop(name, None)
        self._last_error.pop(name, None)
        if adapter is not None:
            try:
                await adapter.disconnect()
            except Exception:
                logger.exception("断开账户 %s 失败", name)

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


def build_adapter(manager: BrokerManager, account_type: str, name: str, params: dict) -> BrokerAdapter:
    """按账户配置实例化适配器。params 为非敏感连接参数，密钥仍走环境变量。"""
    params = params or {}
    if account_type == "paper":
        from app.brokers.paper import PaperBroker

        return PaperBroker(name=name, quote_fn=_live_quote_fn(manager),
                           initial_cash=params.get("initial_cash"))
    if account_type == "futu":
        from app.brokers.futu_adapter import FutuAdapter

        return FutuAdapter(name=name, host=params.get("host"),
                           port=params.get("port"), trd_env=params.get("trd_env"))
    if account_type == "ibkr":
        from app.brokers.ibkr_adapter import IbkrAdapter

        return IbkrAdapter(name=name, host=params.get("host"),
                           port=params.get("port"), client_id=params.get("client_id"))
    raise BrokerError(f"未知券商类型: {account_type}")


def seed_accounts_from_env(db) -> None:
    """账户表为空时按环境变量播种默认账户（保持旧部署行为不变）。"""
    from sqlalchemy import select

    from app.db.models import BrokerAccount

    if db.scalars(select(BrokerAccount)).first() is not None:
        return
    settings = get_settings()
    if settings.paper_enabled:
        db.add(BrokerAccount(name="paper", type="paper", params={}))
    if settings.futu_enabled:
        db.add(BrokerAccount(name="futu", type="futu", params={
            "host": settings.futu_opend_host, "port": settings.futu_opend_port,
            "trd_env": settings.futu_trd_env}))
    if settings.ibkr_enabled:
        db.add(BrokerAccount(name="ibkr", type="ibkr", params={
            "host": settings.ibkr_host, "port": settings.ibkr_port,
            "client_id": settings.ibkr_client_id}))
    db.commit()


def _build_from_settings(manager: BrokerManager) -> None:
    from sqlalchemy import select

    from app.db.base import SessionLocal
    from app.db.models import BrokerAccount

    db = SessionLocal()
    try:
        seed_accounts_from_env(db)
        accounts = db.scalars(select(BrokerAccount).where(BrokerAccount.enabled)).all()
    finally:
        db.close()
    for account in accounts:
        try:
            manager.register(build_adapter(manager, account.type, account.name, account.params))
        except Exception:
            logger.exception("构建券商账户 %s 失败", account.name)


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
