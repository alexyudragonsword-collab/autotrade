"""富途 OpenAPI 适配器。

依赖本地/容器运行的 OpenD 网关（pip install futu-api）。
futu-api 是同步阻塞 SDK，所有调用经 asyncio.to_thread 包装；
推送回调（订单/成交）由 SDK 线程转投主事件循环。
真实环境下单前需 unlock_trade（FUTU_UNLOCK_PWD）。
"""

import asyncio
import logging

from app.brokers.base import BrokerAdapter, BrokerError
from app.config import get_settings
from app.domain.enums import Market, OrderSide, OrderStatus, OrderType
from app.domain.schemas import (
    AccountSnapshot,
    BrokerOrderRef,
    FillEvent,
    OrderRequest,
    OrderUpdate,
    PositionSnapshot,
    Quote,
)

logger = logging.getLogger(__name__)


def _to_futu_market(symbol: str):
    """内部 symbol → (futu TrdMarket, futu code)。futu code 恰好同为 'US.AAPL' 格式。"""
    from futu import TrdMarket

    prefix = symbol.split(".", 1)[0]
    return {
        "US": TrdMarket.US, "HK": TrdMarket.HK,
        "SH": TrdMarket.CN, "SZ": TrdMarket.CN,
    }[prefix], symbol


_FUTU_STATUS_MAP = {
    # futu OrderStatus → 内部 OrderStatus
    "SUBMITTED": OrderStatus.SUBMITTED,
    "SUBMITTING": OrderStatus.SUBMITTED,
    "WAITING_SUBMIT": OrderStatus.SUBMITTED,
    "FILLED_PART": OrderStatus.PARTIALLY_FILLED,
    "FILLED_ALL": OrderStatus.FILLED,
    "CANCELLED_PART": OrderStatus.CANCELLED,
    "CANCELLED_ALL": OrderStatus.CANCELLED,
    "FAILED": OrderStatus.FAILED,
    "SUBMIT_FAILED": OrderStatus.FAILED,
    "DISABLED": OrderStatus.REJECTED,
    "DELETED": OrderStatus.CANCELLED,
}


class FutuAdapter(BrokerAdapter):
    markets = {Market.CN, Market.HK, Market.US}

    def __init__(self, name: str = "futu", host: str | None = None, port: int | None = None,
                 trd_env: str | None = None):
        super().__init__()
        s = get_settings()
        self.name = name
        self.host = host or s.futu_opend_host
        self.port = port or s.futu_opend_port
        self.trd_env = trd_env or s.futu_trd_env
        self._trd_ctx = None
        self._quote_ctx = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._unlocked = False

    # ---------- 连接 ----------

    async def connect(self) -> None:
        try:
            import futu  # noqa: F401
        except ImportError:
            raise BrokerError("未安装 futu-api（pip install 'autotrade[futu]'）")
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> None:
        import futu as ft

        s = get_settings()
        try:
            self._trd_ctx = ft.OpenSecTradeContext(
                filter_trdmarket=ft.TrdMarket.NONE,  # 不过滤，单上下文管理多市场账户
                host=self.host, port=self.port,
                security_firm=ft.SecurityFirm.FUTUSECURITIES,
            )
            self._quote_ctx = ft.OpenQuoteContext(host=self.host, port=self.port)
        except Exception as e:
            self._trd_ctx = self._quote_ctx = None
            raise BrokerError(f"连接 OpenD 失败（{self.host}:{self.port}）: {e}")

        if self.trd_env == "REAL":
            if not s.futu_unlock_pwd:
                raise BrokerError("真实环境需要配置 FUTU_UNLOCK_PWD")
            ret, data = self._trd_ctx.unlock_trade(s.futu_unlock_pwd)
            if ret != ft.RET_OK:
                raise BrokerError(f"交易解锁失败: {data}")
        self._unlocked = True

        adapter = self

        class _OrderHandler(ft.TradeOrderHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret, content = super().on_recv_rsp(rsp_pb)
                if ret == ft.RET_OK and adapter._loop is not None:
                    for _, row in content.iterrows():
                        update = OrderUpdate(
                            broker_order_id=str(row["order_id"]),
                            status=_FUTU_STATUS_MAP.get(str(row["order_status"]), OrderStatus.SUBMITTED),
                            filled_qty=float(row.get("dealt_qty") or 0),
                            avg_fill_price=float(row.get("dealt_avg_price") or 0) or None,
                        )
                        asyncio.run_coroutine_threadsafe(
                            adapter._emit_order_update(update), adapter._loop)
                return ret, content

        class _DealHandler(ft.TradeDealHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret, content = super().on_recv_rsp(rsp_pb)
                if ret == ft.RET_OK and adapter._loop is not None:
                    for _, row in content.iterrows():
                        fill = FillEvent(
                            broker_order_id=str(row["order_id"]),
                            qty=float(row["qty"]), price=float(row["price"]),
                            broker_trade_id=str(row.get("deal_id") or ""),
                        )
                        asyncio.run_coroutine_threadsafe(adapter._emit_fill(fill), adapter._loop)
                return ret, content

        self._trd_ctx.set_handler(_OrderHandler())
        self._trd_ctx.set_handler(_DealHandler())
        logger.info("富途[%s] OpenD 已连接（%s:%s env=%s）", self.name, self.host, self.port, self.trd_env)

    async def disconnect(self) -> None:
        def _close():
            for ctx in (self._trd_ctx, self._quote_ctx):
                if ctx is not None:
                    try:
                        ctx.close()
                    except Exception:
                        pass

        await asyncio.to_thread(_close)
        self._trd_ctx = self._quote_ctx = None
        self._unlocked = False

    def is_connected(self) -> bool:
        return self._trd_ctx is not None and self._unlocked

    # ---------- 交易 ----------

    def _trd_env(self):
        import futu as ft

        return ft.TrdEnv.REAL if self.trd_env == "REAL" else ft.TrdEnv.SIMULATE

    async def place_order(self, req: OrderRequest) -> BrokerOrderRef:
        return await asyncio.to_thread(self._place_order_sync, req)

    def _place_order_sync(self, req: OrderRequest) -> BrokerOrderRef:
        import futu as ft

        if self._trd_ctx is None:
            raise BrokerError("futu 未连接")
        _, code = _to_futu_market(req.symbol)
        side = ft.TrdSide.BUY if req.side == OrderSide.BUY else ft.TrdSide.SELL
        if req.order_type == OrderType.LIMIT:
            order_type, price = ft.OrderType.NORMAL, req.limit_price
        else:
            order_type, price = ft.OrderType.MARKET, 0.0
        ret, data = self._trd_ctx.place_order(
            price=price or 0.0, qty=req.qty, code=code, trd_side=side,
            order_type=order_type, trd_env=self._trd_env(),
        )
        if ret != ft.RET_OK:
            raise BrokerError(f"富途下单失败: {data}")
        return BrokerOrderRef(str(data["order_id"].iloc[0]))

    async def cancel_order(self, broker_order_id: str) -> None:
        def _cancel():
            import futu as ft

            ret, data = self._trd_ctx.modify_order(
                ft.ModifyOrderOp.CANCEL, broker_order_id, 0, 0, trd_env=self._trd_env())
            if ret != ft.RET_OK:
                raise BrokerError(f"富途撤单失败: {data}")

        await asyncio.to_thread(_cancel)

    async def get_order(self, broker_order_id: str) -> OrderUpdate | None:
        def _query():
            import futu as ft

            ret, data = self._trd_ctx.order_list_query(
                order_id=broker_order_id, trd_env=self._trd_env())
            if ret != ft.RET_OK or data.empty:
                return None
            row = data.iloc[0]
            return OrderUpdate(
                broker_order_id=str(row["order_id"]),
                status=_FUTU_STATUS_MAP.get(str(row["order_status"]), OrderStatus.SUBMITTED),
                filled_qty=float(row.get("dealt_qty") or 0),
                avg_fill_price=float(row.get("dealt_avg_price") or 0) or None,
            )

        return await asyncio.to_thread(_query)

    async def get_positions(self) -> list[PositionSnapshot]:
        def _query():
            import futu as ft

            ret, data = self._trd_ctx.position_list_query(trd_env=self._trd_env())
            if ret != ft.RET_OK:
                raise BrokerError(f"查询持仓失败: {data}")
            out = []
            for _, row in data.iterrows():
                code = str(row["code"])
                prefix = code.split(".", 1)[0]
                market = {"US": Market.US, "HK": Market.HK,
                          "SH": Market.CN, "SZ": Market.CN}.get(prefix, Market.US)
                out.append(PositionSnapshot(code, market, float(row["qty"]),
                                            float(row.get("cost_price") or 0)))
            return out

        return await asyncio.to_thread(_query)

    async def get_account(self) -> AccountSnapshot:
        def _query():
            import futu as ft

            ret, data = self._trd_ctx.accinfo_query(trd_env=self._trd_env())
            if ret != ft.RET_OK:
                raise BrokerError(f"查询账户失败: {data}")
            row = data.iloc[0]
            return AccountSnapshot(
                cash=float(row.get("cash") or 0),
                net_value=float(row.get("total_assets") or 0),
                buying_power=float(row.get("power") or 0),
            )

        return await asyncio.to_thread(_query)

    async def get_quote(self, symbol: str) -> Quote | None:
        def _query():
            import futu as ft

            if self._quote_ctx is None:
                return None
            ret, data = self._quote_ctx.get_market_snapshot([symbol])
            if ret != ft.RET_OK or data.empty:
                return None
            return Quote(symbol, float(data.iloc[0]["last_price"]))

        try:
            return await asyncio.to_thread(_query)
        except Exception:
            logger.warning("富途行情获取失败: %s", symbol)
            return None
