"""盈透 IBKR 适配器。

依赖运行中的 TWS 或 IB Gateway（paper 端口 7497 / 实盘 7496）。
使用 ib_async（ib_insync 的社区维护 fork），原生 asyncio。
内置简单令牌桶节流应对 IB pacing 限制；Gateway 每日重启由
BrokerManager.health_check 自动重连兜底。
"""

import asyncio
import logging
import time as _time

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

_IB_STATUS_MAP = {
    "PendingSubmit": OrderStatus.SUBMITTED,
    "PreSubmitted": OrderStatus.SUBMITTED,
    "Submitted": OrderStatus.SUBMITTED,
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELLED,
    "ApiCancelled": OrderStatus.CANCELLED,
    "Inactive": OrderStatus.REJECTED,
}


class _Throttle:
    """令牌桶：限制每秒消息数，规避 IB pacing violation。"""

    def __init__(self, rate: float = 40.0):
        self.rate = rate
        self._allowance = rate
        self._last = _time.monotonic()

    async def acquire(self) -> None:
        while True:
            now = _time.monotonic()
            self._allowance = min(self.rate, self._allowance + (now - self._last) * self.rate)
            self._last = now
            if self._allowance >= 1:
                self._allowance -= 1
                return
            await asyncio.sleep((1 - self._allowance) / self.rate)


class IbkrAdapter(BrokerAdapter):
    markets = {Market.US}

    def __init__(self, name: str = "ibkr", host: str | None = None, port: int | None = None,
                 client_id: int | None = None):
        super().__init__()
        s = get_settings()
        self.name = name
        self.host = host or s.ibkr_host
        self.port = port or s.ibkr_port
        self.client_id = client_id if client_id is not None else s.ibkr_client_id
        self._ib = None
        self._throttle = _Throttle()

    # ---------- 连接 ----------

    async def connect(self) -> None:
        try:
            from ib_async import IB
        except ImportError:
            raise BrokerError("未安装 ib_async（pip install 'autotrade[ibkr]'）")
        ib = IB()
        try:
            await ib.connectAsync(self.host, self.port, clientId=self.client_id, timeout=10)
        except Exception as e:
            raise BrokerError(f"连接 TWS/Gateway 失败（{self.host}:{self.port}）: {e}")
        self._ib = ib
        ib.orderStatusEvent += self._on_ib_order_status
        ib.execDetailsEvent += self._on_ib_exec
        ib.commissionReportEvent += self._on_ib_commission
        ib.disconnectedEvent += lambda: logger.warning("IBKR 连接断开，等待健康检查自动重连")
        logger.info("IBKR[%s] 已连接（%s:%s clientId=%s）", self.name, self.host, self.port, self.client_id)

    async def disconnect(self) -> None:
        if self._ib is not None:
            self._ib.disconnect()
            self._ib = None

    def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    # ---------- 事件桥接 ----------

    def _on_ib_order_status(self, trade) -> None:
        status = _IB_STATUS_MAP.get(trade.orderStatus.status)
        if status is None:
            return
        filled = float(trade.orderStatus.filled or 0)
        if status == OrderStatus.SUBMITTED and filled > 0:
            status = OrderStatus.PARTIALLY_FILLED
        update = OrderUpdate(
            broker_order_id=str(trade.order.orderId),
            status=status,
            filled_qty=filled,
            avg_fill_price=float(trade.orderStatus.avgFillPrice or 0) or None,
        )
        asyncio.ensure_future(self._emit_order_update(update))

    def _on_ib_exec(self, trade, fill) -> None:
        event = FillEvent(
            broker_order_id=str(trade.order.orderId),
            qty=float(fill.execution.shares),
            price=float(fill.execution.price),
            fee=0.0,  # 佣金稍后由 commissionReport 回填
            broker_trade_id=str(fill.execution.execId),
        )
        asyncio.ensure_future(self._emit_fill(event))

    def _on_ib_commission(self, trade, fill, report) -> None:
        """commissionReport 晚于成交回报到达 → 回填 TradeFill 的佣金与已实现盈亏。"""
        asyncio.ensure_future(asyncio.to_thread(
            self._update_fill_commission,
            str(fill.execution.execId),
            float(report.commission or 0),
            float(report.realizedPNL) if getattr(report, "realizedPNL", None) not in (None, 0) else None,
        ))

    @staticmethod
    def _update_fill_commission(exec_id: str, commission: float, realized_pnl: float | None) -> None:
        from sqlalchemy import select

        from app.db.base import SessionLocal
        from app.db.models import TradeFill

        db = SessionLocal()
        try:
            fill_row = db.scalar(select(TradeFill).where(TradeFill.broker_trade_id == exec_id)
                                 .order_by(TradeFill.id.desc()))
            if fill_row is None:
                return
            fill_row.fee = commission
            # IB 的 realizedPNL 为魔数 1.7976931348623157e+308 时表示无效
            if realized_pnl is not None and abs(realized_pnl) < 1e300:
                fill_row.realized_pnl = realized_pnl
            db.commit()
        except Exception:
            logger.exception("回填 IBKR 佣金失败: %s", exec_id)
        finally:
            db.close()

    # ---------- 交易 ----------

    @staticmethod
    def _contract(symbol: str):
        from ib_async import Option, Stock

        from app.domain.contracts import OptionContract

        oc = OptionContract.parse(symbol)
        if oc is not None:
            return Option(oc.ticker, oc.expiry, oc.strike, oc.right, "SMART", currency="USD")
        ticker = symbol.split(".", 1)[-1]
        return Stock(ticker, "SMART", "USD")

    async def _qualified(self, symbol: str):
        """期权合约先 qualify（拿 conId），按 symbol 缓存节省 pacing 预算。"""
        from app.domain.contracts import is_option

        contract = self._contract(symbol)
        if not is_option(symbol):
            return contract
        if not hasattr(self, "_qualified_cache"):
            self._qualified_cache: dict[str, object] = {}
        cached = self._qualified_cache.get(symbol)
        if cached is not None:
            return cached
        await self._throttle.acquire()
        result = await self._ib.qualifyContractsAsync(contract)
        if not result:
            raise BrokerError(f"IBKR 无法识别期权合约 {symbol}")
        self._qualified_cache[symbol] = result[0]
        return result[0]

    async def place_order(self, req: OrderRequest) -> BrokerOrderRef:
        from ib_async import LimitOrder, MarketOrder

        if self._ib is None:
            raise BrokerError("ibkr 未连接")
        contract = await self._qualified(req.symbol)
        await self._throttle.acquire()
        action = "BUY" if req.side == OrderSide.BUY else "SELL"
        if req.order_type == OrderType.LIMIT:
            order = LimitOrder(action, req.qty, req.limit_price)
        else:
            order = MarketOrder(action, req.qty)
        trade = self._ib.placeOrder(contract, order)
        return BrokerOrderRef(str(trade.order.orderId))

    async def cancel_order(self, broker_order_id: str) -> None:
        if self._ib is None:
            raise BrokerError("ibkr 未连接")
        await self._throttle.acquire()
        for trade in self._ib.openTrades():
            if str(trade.order.orderId) == broker_order_id:
                self._ib.cancelOrder(trade.order)
                return
        raise BrokerError(f"未找到可撤订单 {broker_order_id}")

    async def get_order(self, broker_order_id: str) -> OrderUpdate | None:
        if self._ib is None:
            return None
        for trade in self._ib.trades():
            if str(trade.order.orderId) == broker_order_id:
                status = _IB_STATUS_MAP.get(trade.orderStatus.status, OrderStatus.SUBMITTED)
                filled = float(trade.orderStatus.filled or 0)
                if status == OrderStatus.SUBMITTED and filled > 0:
                    status = OrderStatus.PARTIALLY_FILLED
                return OrderUpdate(broker_order_id, status, filled,
                                   float(trade.orderStatus.avgFillPrice or 0) or None)
        return None

    async def get_positions(self) -> list[PositionSnapshot]:
        from app.domain.contracts import OptionContract

        if self._ib is None:
            raise BrokerError("ibkr 未连接")
        await self._throttle.acquire()
        positions = await self._ib.reqPositionsAsync()
        out = []
        for pos in positions:
            sec = pos.contract.secType
            if sec == "STK":
                out.append(PositionSnapshot(
                    f"US.{pos.contract.symbol}", Market.US,
                    float(pos.position), float(pos.avgCost or 0)))
            elif sec == "OPT":
                mult = float(pos.contract.multiplier or 100)
                oc = OptionContract(
                    underlying=f"US.{pos.contract.symbol}",
                    expiry=str(pos.contract.lastTradeDateOrContractMonth)[:8],
                    right=str(pos.contract.right)[:1].upper(),
                    strike=float(pos.contract.strike))
                # IB 的 avgCost 是每张合约成本（含乘数）→ 统一为每股口径
                out.append(PositionSnapshot(
                    oc.symbol(), Market.US, float(pos.position),
                    float(pos.avgCost or 0) / mult if mult else 0.0,
                    multiplier=mult))
        return out

    async def get_account(self) -> AccountSnapshot:
        if self._ib is None:
            raise BrokerError("ibkr 未连接")
        await self._throttle.acquire()
        values = self._ib.accountValues()
        acc = {v.tag: v.value for v in values if v.currency in ("USD", "")}
        return AccountSnapshot(
            cash=float(acc.get("TotalCashValue", 0) or 0),
            net_value=float(acc.get("NetLiquidation", 0) or 0),
            buying_power=float(acc.get("BuyingPower", 0) or 0),
        )

    # ---------- 期权链 ----------

    async def _opt_params(self, underlying: str):
        """reqSecDefOptParams 结果（SMART），按标的缓存 10 分钟。"""
        import time as _t

        from ib_async import Stock

        if not hasattr(self, "_chain_cache"):
            self._chain_cache: dict[str, tuple[float, object]] = {}
        cached = self._chain_cache.get(underlying)
        if cached is not None and _t.monotonic() - cached[0] < 600:
            return cached[1]
        ticker = underlying.split(".", 1)[-1]
        stock = Stock(ticker, "SMART", "USD")
        await self._throttle.acquire()
        qualified = await self._ib.qualifyContractsAsync(stock)
        if not qualified:
            raise BrokerError(f"IBKR 无法识别标的 {underlying}")
        stock = qualified[0]
        await self._throttle.acquire()
        chains = await self._ib.reqSecDefOptParamsAsync(
            stock.symbol, "", stock.secType, stock.conId)
        chain = next((c for c in chains if c.exchange == "SMART"), chains[0] if chains else None)
        if chain is None:
            raise BrokerError(f"{underlying} 无期权链")
        self._chain_cache[underlying] = (_t.monotonic(), chain)
        return chain

    async def get_option_expirations(self, underlying: str) -> list[str]:
        if self._ib is None:
            raise BrokerError("ibkr 未连接")
        chain = await self._opt_params(underlying)
        return sorted(chain.expirations)

    async def get_option_chain(self, underlying: str, expiry: str,
                               with_quotes: bool = False,
                               strikes_around: int | None = None):
        from app.domain.contracts import OptionContract
        from app.domain.schemas import OptionChainItem

        if self._ib is None:
            raise BrokerError("ibkr 未连接")
        chain = await self._opt_params(underlying)
        if expiry not in chain.expirations:
            raise BrokerError(f"{underlying} 无 {expiry} 到期的期权")
        strikes = sorted(chain.strikes)
        mult = float(chain.multiplier or 100)

        # 限幅：以正股现价为中心取 N 档行权价（保护 pacing 预算）
        if strikes_around:
            center = None
            quote = await self.get_quote(underlying)
            if quote is not None:
                center = quote.price
            if center is not None and strikes:
                strikes.sort(key=lambda s: abs(s - center))
                strikes = sorted(strikes[:strikes_around])

        items: list[OptionChainItem] = []
        for strike in strikes:
            for right in ("C", "P"):
                oc = OptionContract(underlying=underlying, expiry=expiry,
                                    right=right, strike=strike)
                items.append(OptionChainItem(symbol=oc.symbol(), strike=strike,
                                             right=right, multiplier=mult))

        if with_quotes and items:
            contracts = []
            for item in items:
                try:
                    contracts.append(await self._qualified(item.symbol))
                except BrokerError:
                    contracts.append(None)
            valid = [(i, c) for i, c in zip(items, contracts) if c is not None]
            for batch_start in range(0, len(valid), 50):
                batch = valid[batch_start:batch_start + 50]
                await self._throttle.acquire()
                tickers = await self._ib.reqTickersAsync(*[c for _, c in batch])
                for (item, _), tk in zip(batch, tickers):
                    def _num(v):
                        return float(v) if v is not None and v == v and v > 0 else None
                    item.bid = _num(getattr(tk, "bid", None))
                    item.ask = _num(getattr(tk, "ask", None))
                    item.last = _num(getattr(tk, "last", None)) or _num(getattr(tk, "close", None))
        return items

    async def get_contract_multiplier(self, symbol: str) -> float | None:
        from app.domain.contracts import OptionContract

        oc = OptionContract.parse(symbol)
        if oc is None or self._ib is None:
            return None
        try:
            chain = await self._opt_params(oc.underlying)
            return float(chain.multiplier or 100)
        except Exception:
            return None

    async def get_quote(self, symbol: str) -> Quote | None:
        if self._ib is None:
            return None
        try:
            contract = await self._qualified(symbol)
            await self._throttle.acquire()
            ticker = self._ib.reqMktData(contract, snapshot=True)
            for _ in range(20):  # 最多等 2 秒
                await asyncio.sleep(0.1)
                price = ticker.marketPrice()
                if price and price == price:  # 非 NaN
                    return Quote(symbol, float(price))
            return None
        except Exception:
            logger.warning("IBKR 行情获取失败: %s", symbol)
            return None
