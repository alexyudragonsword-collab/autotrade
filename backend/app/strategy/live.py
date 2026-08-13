"""本地策略实盘驱动：LiveContext + 定时/手动运行。

与回测共用同一份 Strategy.on_bar 代码；buy/sell 产生的动作被转成
NormalizedSignal 送入与 TradingView 告警完全相同的管道
（去重 → 策略配置 → 风控 → 下单 → 通知），mode=signal_only 时同样只提醒。

去重键按「策略|标的|动作|最后一根K线日期」构造：同一根日线上重复运行
（手动补跑、调度重叠）不会重复下单。
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import select

from app.data.store import get_bar_store
from app.db.base import SessionLocal
from app.db.models import Position, StrategyConfig
from app.domain.enums import Market, OrderType, SignalAction
from app.domain.schemas import NormalizedSignal
from app.strategy.base import StrategyContext
from app.strategy.registry import get_strategy_class

logger = logging.getLogger(__name__)

_LOOKBACK_DAYS = 420  # 与选股器一致，足够 250 日均线


def _market_of(symbol: str) -> Market:
    prefix = symbol.split(".", 1)[0]
    return {"US": Market.US, "HK": Market.HK, "SH": Market.CN, "SZ": Market.CN}[prefix]


@dataclass
class _Action:
    action: SignalAction
    qty: float
    limit: float | None


class LiveContext(StrategyContext):
    """实盘上下文：行情来自 BarStore，持仓来自本地 Position 镜像。"""

    def __init__(self, symbol: str, bars: pd.DataFrame, position_qty: float):
        self.symbol = symbol
        self._bars = bars
        self._position_qty = position_qty
        self.actions: list[_Action] = []
        self.logs: list[str] = []

    def history(self, n: int) -> pd.DataFrame:
        return self._bars.iloc[-n:]

    def position(self) -> float:
        return self._position_qty

    def buy(self, qty: float, limit: float | None = None) -> None:
        if qty > 0:
            self.actions.append(_Action(SignalAction.BUY, qty, limit))

    def sell(self, qty: float, limit: float | None = None) -> None:
        if qty > 0:
            self.actions.append(_Action(SignalAction.SELL, qty, limit))

    def log(self, msg: str) -> None:
        self.logs.append(msg)
        logger.info("[live:%s] %s", self.symbol, msg)


async def run_strategy_live(strategy_id: int) -> dict:
    """运行一个本地策略：逐标的刷新行情 → on_bar → 动作转信号入管道。

    返回摘要 {"symbols": n, "signals": n, "errors": [...]}，供 API/日志使用。
    """
    db = SessionLocal()
    try:
        cfg = db.get(StrategyConfig, strategy_id)
        if cfg is None:
            raise ValueError(f"策略 {strategy_id} 不存在")
        if not cfg.enabled:
            return {"symbols": 0, "signals": 0, "errors": ["策略已停用"]}
        if not cfg.class_name:
            raise ValueError("该策略未绑定本地策略类（仅接收 TV 信号）")
        if not cfg.symbols:
            return {"symbols": 0, "signals": 0, "errors": ["未配置监控标的"]}

        strategy_cls = get_strategy_class(cfg.class_name)
        store = get_bar_store()
        timeframe = cfg.timeframe or "1d"
        lookback_days = _LOOKBACK_DAYS if timeframe == "1d" else 50  # 分钟线源只有近期数据
        end = date.today().isoformat()
        start = (date.today() - timedelta(days=lookback_days)).isoformat()

        from app.strategy.options import OptionStrategy
        from app.strategy.portfolio import PortfolioStrategy

        if issubclass(strategy_cls, OptionStrategy):
            return await _run_option_live(db, cfg, strategy_cls)
        if issubclass(strategy_cls, PortfolioStrategy):
            return await _run_portfolio_live(db, cfg, strategy_cls, store, start, end, timeframe)

        emitted = 0
        errors: list[str] = []
        for symbol in cfg.symbols:
            try:
                market = _market_of(symbol)
            except KeyError:
                errors.append(f"{symbol}: 无法识别市场前缀")
                continue
            try:
                bars = store.get_bars(market, symbol, start, end, interval=timeframe)
            except Exception as e:
                errors.append(f"{symbol}: 行情获取失败 {e}")
                continue
            if bars.empty or len(bars) < 2:
                errors.append(f"{symbol}: 无足够行情数据")
                continue

            pos = db.scalar(select(Position).where(Position.broker == cfg.broker,
                                                   Position.symbol == symbol))
            ctx = LiveContext(symbol, bars, pos.qty if pos else 0.0)
            strategy = strategy_cls(**(cfg.params or {}))
            try:
                strategy.on_bar(ctx)
            except Exception as e:
                logger.exception("策略 %s 在 %s 上执行失败", cfg.name, symbol)
                errors.append(f"{symbol}: on_bar 异常 {e}")
                continue

            last_bar_date = bars.index[-1].isoformat()
            last_close = float(bars["close"].iloc[-1])
            for act in ctx.actions:
                emitted += 1
                await _emit(cfg, symbol, market, act, last_bar_date, last_close)

        return {"symbols": len(cfg.symbols), "signals": emitted, "errors": errors}
    finally:
        db.close()


async def _run_portfolio_live(db, cfg: StrategyConfig, strategy_cls, store,
                              start: str, end: str, timeframe: str) -> dict:
    """组合策略实盘：on_rebalance 给出目标市值 → 与当前持仓差额 → 买卖信号入管道。"""
    from app.brokers.manager import get_broker_manager
    from app.strategy.portfolio import PortfolioContext

    adapter = get_broker_manager().get_if_connected(cfg.broker)
    if adapter is None:
        return {"symbols": len(cfg.symbols), "signals": 0,
                "errors": [f"券商 {cfg.broker} 离线，无法获取账户权益"]}
    account = await adapter.get_account()

    bars_map: dict[str, pd.DataFrame] = {}
    errors: list[str] = []
    for symbol in cfg.symbols:
        try:
            bars = store.get_bars(_market_of(symbol), symbol, start, end, interval=timeframe)
        except Exception as e:
            errors.append(f"{symbol}: 行情获取失败 {e}")
            continue
        if not bars.empty:
            bars_map[symbol] = bars
    if not bars_map:
        return {"symbols": len(cfg.symbols), "signals": 0, "errors": errors + ["无任何行情数据"]}

    positions = {
        p.symbol: p.qty
        for p in db.scalars(select(Position).where(Position.broker == cfg.broker)).all()
    }

    class _LivePortfolioContext(PortfolioContext):
        def __init__(self):
            self.symbols = list(bars_map)
            self.targets: dict[str, float] = {}
            self.logs: list[str] = []

        def history(self, symbol, n):
            return bars_map[symbol].iloc[-n:]

        def position(self, symbol):
            return positions.get(symbol, 0.0)

        def price(self, symbol):
            return float(bars_map[symbol]["close"].iloc[-1])

        def equity(self):
            return account.net_value

        def order_target_value(self, symbol, value):
            self.targets[symbol] = value

        def log(self, msg):
            self.logs.append(msg)
            logger.info("[live-portfolio:%s] %s", cfg.name, msg)

    ctx = _LivePortfolioContext()
    strategy = strategy_cls(**(cfg.params or {}))
    strategy.on_rebalance(ctx)

    emitted = 0
    for symbol, target_value in ctx.targets.items():
        price = ctx.price(symbol)
        if price <= 0:
            continue
        target_qty = int(target_value / price)
        diff = target_qty - positions.get(symbol, 0.0)
        if diff == 0:
            continue
        action = SignalAction.BUY if diff > 0 else SignalAction.SELL
        act = _Action(action, abs(diff), None)
        bar_ts = bars_map[symbol].index[-1].isoformat()
        await _emit(cfg, symbol, _market_of(symbol), act, bar_ts, price)
        emitted += 1

    return {"symbols": len(cfg.symbols), "signals": emitted, "errors": errors}


async def _emit(cfg: StrategyConfig, symbol: str, market: Market,
                act: _Action, bar_date: str, last_close: float) -> None:
    from app.signals.pipeline import get_pipeline

    signal = NormalizedSignal(
        source="strategy",
        dedup_key=f"strat:{cfg.name}:{symbol}:{act.action}:{bar_date}",
        strategy=cfg.name,
        symbol=symbol,
        market=market,
        action=act.action,
        quantity=act.qty,
        order_type=OrderType.LIMIT if act.limit is not None else OrderType.MARKET,
        price=act.limit if act.limit is not None else last_close,
        raw={"driver": "local", "class": cfg.class_name, "bar_date": bar_date},
    )
    await get_pipeline().process(signal)


async def _run_option_live(db, cfg: StrategyConfig, strategy_cls) -> dict:
    """期权策略实盘：逐标的构建 LiveOptionContext → async on_run → 动作转信号入管道。

    期权链来源：执行账户自身支持则用之；否则借任一在线真实券商的链
    （paper 执行 + 真实链报价 = 推荐的模拟验证姿势）。
    """
    from app.brokers.base import BrokerError
    from app.brokers.manager import get_broker_manager
    from app.domain.contracts import OptionContract, underlying_of
    from app.strategy.options import OptionAction, OptionPosition, OptionStrategyContext

    manager = get_broker_manager()
    exec_adapter = manager.get_if_connected(cfg.broker)
    if exec_adapter is None:
        return {"symbols": len(cfg.symbols), "signals": 0,
                "errors": [f"执行账户 {cfg.broker} 离线"]}

    # 链数据源候选：执行账户优先，然后任一其它在线账户（真实券商能提供链）
    chain_candidates = [exec_adapter] + [
        a for n, a in manager._adapters.items()
        if n != cfg.broker and a.is_connected()
    ]

    try:
        account = await exec_adapter.get_account()
        account_cash = account.cash
    except Exception:
        account_cash = None

    positions = db.scalars(select(Position).where(Position.broker == cfg.broker)).all()

    class _LiveOptionContext(OptionStrategyContext):
        def __init__(self, underlying: str):
            self.underlying = underlying
            self.actions: list[OptionAction] = []
            self.logs: list[str] = []
            self._spot_cache: float | None = None
            self._exp_cache: list[str] | None = None

        async def spot(self):
            if self._spot_cache is None:
                for adapter in chain_candidates:
                    try:
                        quote = await adapter.get_quote(self.underlying)
                        if quote is not None and quote.price:
                            self._spot_cache = float(quote.price)
                            break
                    except Exception:
                        continue
            return self._spot_cache

        def stock_qty(self):
            row = next((p for p in positions if p.symbol == self.underlying), None)
            return row.qty if row else 0.0

        def cash(self):
            return account_cash

        def option_positions(self):
            out = []
            for p in positions:
                oc = OptionContract.parse(p.symbol)
                if oc is not None and oc.underlying == self.underlying and p.qty != 0:
                    out.append(OptionPosition(contract=oc, symbol=p.symbol, qty=p.qty,
                                              avg_cost=p.avg_cost,
                                              multiplier=p.multiplier or 1.0))
            return out

        async def expirations(self):
            if self._exp_cache is None:
                for adapter in chain_candidates:
                    try:
                        self._exp_cache = await adapter.get_option_expirations(self.underlying)
                        break
                    except (BrokerError, NotImplementedError):
                        continue
                    except Exception:
                        logger.exception("查询 %s 到期日失败", self.underlying)
                        continue
                if self._exp_cache is None:
                    self._exp_cache = []
            return self._exp_cache

        async def select_contract(self, right, min_dte, max_dte, otm_pct):
            from app.domain.contracts import days_to_expiry as _dte

            spot_price = await self.spot()
            if not spot_price:
                self.log("无法获取正股价格")
                return None
            target_expiry = None
            for expiry in await self.expirations():
                dte = _dte(f"{self.underlying}|{expiry}|C|1")
                if dte is not None and min_dte <= dte <= max_dte:
                    target_expiry = expiry
                    break
            if target_expiry is None:
                return None
            chain = None
            for adapter in chain_candidates:
                try:
                    chain = await adapter.get_option_chain(
                        self.underlying, target_expiry, with_quotes=True, strikes_around=30)
                    break
                except (BrokerError, NotImplementedError):
                    continue
                except Exception:
                    logger.exception("查询 %s 期权链失败", self.underlying)
                    continue
            if not chain:
                return None
            if right == "C":
                floor_price = spot_price * (1 + otm_pct / 100)
                candidates = sorted((i for i in chain
                                     if i.right == "C" and i.strike >= floor_price),
                                    key=lambda i: i.strike)
            else:
                cap_price = spot_price * (1 - otm_pct / 100)
                candidates = sorted((i for i in chain
                                     if i.right == "P" and i.strike <= cap_price),
                                    key=lambda i: -i.strike)
            return candidates[0] if candidates else None

        def sell_open(self, symbol, qty, price=None, multiplier=100.0):
            if qty > 0:
                self.actions.append(OptionAction(symbol, "sell", qty, price, multiplier))

        def buy_close(self, symbol, qty, price=None, multiplier=100.0):
            if qty > 0:
                self.actions.append(OptionAction(symbol, "buy", qty, price, multiplier))

        def log(self, msg):
            self.logs.append(msg)
            logger.info("[option:%s:%s] %s", cfg.name, self.underlying, msg)

    from datetime import date as _date

    from app.signals.pipeline import get_pipeline

    emitted = 0
    errors: list[str] = []
    today = _date.today().isoformat()
    for underlying in cfg.symbols:
        if "|" in underlying:
            errors.append(f"{underlying}: 期权策略的标的应填正股符号")
            continue
        ctx = _LiveOptionContext(underlying)
        strategy = strategy_cls(**(cfg.params or {}))
        try:
            await strategy.on_run(ctx)
        except Exception as e:
            logger.exception("期权策略 %s 在 %s 上执行失败", cfg.name, underlying)
            errors.append(f"{underlying}: on_run 异常 {e}")
            continue
        for act in ctx.actions:
            signal = NormalizedSignal(
                source="strategy",
                dedup_key=f"strat:{cfg.name}:{act.symbol}:{act.action}:{today}",
                strategy=cfg.name,
                symbol=act.symbol,
                market=_market_of(underlying_of(act.symbol)),
                action=SignalAction(act.action),
                quantity=act.qty,
                order_type=OrderType.MARKET,
                price=act.price,
                raw={"driver": "option_strategy", "class": cfg.class_name},
            )
            await get_pipeline().process(signal)
            emitted += 1
    return {"symbols": len(cfg.symbols), "signals": emitted, "errors": errors}
