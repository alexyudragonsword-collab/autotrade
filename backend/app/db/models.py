from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(128))


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | str | None] = mapped_column(JSON, nullable=True)


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16), default="tradingview")
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    dedup_key: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    strategy_name: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    market: Mapped[str] = mapped_column(String(8))
    action: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    order_type: Mapped[str] = mapped_column(String(8), default="market")
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="received", index=True)
    reject_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True, index=True)
    broker: Mapped[str] = mapped_column(String(16), index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    market: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(8))
    qty: Mapped[float] = mapped_column(Float)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
    avg_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending_submit", index=True)
    error_msg: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TradeFill(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    broker_trade_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qty: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    # 卖出成交时按当时持仓均价计算并落库（买入为 NULL）；风控日亏损直接累加该列
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Position(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(primary_key=True)
    broker: Mapped[str] = mapped_column(String(16), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    market: Mapped[str] = mapped_column(String(8))
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    avg_cost: Mapped[float] = mapped_column(Float, default=0.0)
    last_sync_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StrategyConfig(Base):
    __tablename__ = "strategy_configs"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    class_name: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 本地策略类；TV 信号可为空
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    mode: Mapped[str] = mapped_column(String(16), default="signal_only")
    broker: Mapped[str] = mapped_column(String(16), default="paper")
    default_qty: Mapped[float] = mapped_column(Float, default=0.0)  # 信号未带 qty 时的默认数量
    # 本地策略实盘驱动：监控标的与运行时间（cron，北京时间）；仅 class_name 非空时生效
    symbols: Mapped[list] = mapped_column(JSON, default=list)
    schedule_cron: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScreenerConfig(Base):
    __tablename__ = "screener_configs"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    market: Mapped[str] = mapped_column(String(8), default="CN")
    universe: Mapped[dict] = mapped_column(JSON, default=dict)  # {"type": "index"|"symbols", "value": ...}
    rules: Mapped[dict] = mapped_column(JSON, default=dict)  # {"conditions": [...], "sort_by": ..., "limit": N}
    schedule_cron: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScreenResult(Base):
    __tablename__ = "screen_results"
    id: Mapped[int] = mapped_column(primary_key=True)
    screener_id: Mapped[int] = mapped_column(ForeignKey("screener_configs.id"), index=True)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    symbols: Mapped[list] = mapped_column(JSON, default=list)  # [{"symbol":..., 指标列...}]
    count: Mapped[int] = mapped_column(Integer, default=0)
    error_msg: Mapped[str | None] = mapped_column(String(300), nullable=True)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_class: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    symbols: Mapped[list] = mapped_column(JSON, default=list)
    market: Mapped[str] = mapped_column(String(8), default="US")
    start_date: Mapped[str] = mapped_column(String(10))
    end_date: Mapped[str] = mapped_column(String(10))
    initial_cash: Mapped[float] = mapped_column(Float, default=100000.0)
    commission_bps: Mapped[float] = mapped_column(Float, default=3.0)
    slippage_bps: Mapped[float] = mapped_column(Float, default=1.0)
    group_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)  # 参数扫描分组
    status: Mapped[str] = mapped_column(String(10), default="queued", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    equity_curve: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [[date, equity], ...]
    trades: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class RiskConfig(Base):
    """全局风控配置，单行（id=1）。"""

    __tablename__ = "risk_config"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    trading_enabled: Mapped[bool] = mapped_column(Boolean, default=True)  # kill switch：False=全局禁止下单
    max_order_value: Mapped[float] = mapped_column(Float, default=50000.0)  # 单笔订单金额上限
    max_position_value_per_symbol: Mapped[float] = mapped_column(Float, default=100000.0)
    max_total_exposure: Mapped[float] = mapped_column(Float, default=500000.0)
    max_orders_per_day: Mapped[int] = mapped_column(Integer, default=50)
    max_daily_loss: Mapped[float] = mapped_column(Float, default=20000.0)  # 当日已实现亏损上限
    symbol_whitelist: Mapped[list] = mapped_column(JSON, default=list)  # 空 = 不限制
    trading_hours_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RiskEventLog(Base):
    __tablename__ = "risk_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    rule_name: Mapped[str] = mapped_column(String(64))
    order_intent: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    decision: Mapped[str] = mapped_column(String(8))  # allow | block
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AuditLog(Base):
    """管理后台写操作审计（POST/PUT/DELETE）。"""

    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)  # 截断后的请求体摘要
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class NotifyChannel(Base):
    __tablename__ = "notify_channels"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(16))  # telegram | email | wecom | dingtalk
    name: Mapped[str] = mapped_column(String(64), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    min_level: Mapped[str] = mapped_column(String(8), default="info")
    # 非敏感展示信息；密钥一律走环境变量
    config: Mapped[dict] = mapped_column(JSON, default=dict)
