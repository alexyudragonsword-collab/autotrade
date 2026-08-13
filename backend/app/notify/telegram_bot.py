"""Telegram 双向指令机器人。

getUpdates 长轮询（无需公网回调地址），仅响应配置的 TELEGRAM_CHAT_ID，
其它会话一律忽略——chat_id 即鉴权。

指令：
  /help        指令列表
  /status      账户净值、券商状态、今日信号/订单/拦截、交易开关
  /pos         当前持仓
  /orders      最近 5 笔订单
  /strategies  策略列表
  /run 策略名   立即运行本地策略
  /kill        紧急停止全部交易（kill switch）
  /resume      恢复交易
"""

import asyncio
import logging
from datetime import datetime, time, timezone

import httpx
from sqlalchemy import func, select

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models import Order, Position, RiskConfig, RiskEventLog, Signal, StrategyConfig

logger = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._offset = 0

    # ---------- 生命周期 ----------

    def start(self) -> None:
        s = get_settings()
        if not s.telegram_bot_token or not s.telegram_chat_id:
            logger.info("Telegram 未配置，指令机器人不启动")
            return
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Telegram 指令机器人已启动")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ---------- 轮询 ----------

    @property
    def _api(self) -> str:
        return f"https://api.telegram.org/bot{get_settings().telegram_bot_token}"

    async def _poll_loop(self) -> None:
        async with httpx.AsyncClient(timeout=35) as client:
            while True:
                try:
                    resp = await client.get(f"{self._api}/getUpdates",
                                            params={"offset": self._offset + 1, "timeout": 25})
                    for update in resp.json().get("result", []):
                        self._offset = max(self._offset, update["update_id"])
                        await self._handle_update(client, update)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("Telegram 轮询异常（10 秒后重试）: %s", e)
                    await asyncio.sleep(10)

    async def _handle_update(self, client: httpx.AsyncClient, update: dict) -> None:
        msg = update.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text:
            return
        if chat_id != str(get_settings().telegram_chat_id):
            logger.warning("忽略未授权 Telegram 会话 %s 的指令", chat_id)
            return
        try:
            reply = await self.handle_command(text)
        except Exception as e:
            logger.exception("处理 Telegram 指令失败: %s", text)
            reply = f"❌ 执行失败: {e}"
        if reply:
            await client.post(f"{self._api}/sendMessage",
                              json={"chat_id": chat_id, "text": reply})

    # ---------- 指令 ----------

    async def handle_command(self, text: str) -> str:
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()  # 支持 /cmd@botname
        args = parts[1:]
        handlers = {
            "/help": self._cmd_help, "/start": self._cmd_help,
            "/status": self._cmd_status, "/pos": self._cmd_pos,
            "/orders": self._cmd_orders, "/strategies": self._cmd_strategies,
        }
        if cmd in handlers:
            return await handlers[cmd]()
        if cmd == "/run":
            return await self._cmd_run(args)
        if cmd == "/kill":
            return await self._cmd_kill_switch(False)
        if cmd == "/resume":
            return await self._cmd_kill_switch(True)
        return "未知指令，/help 查看可用指令"

    async def _cmd_help(self) -> str:
        return (
            "🤖 AutoTrade 指令\n"
            "/status - 账户与今日概览\n"
            "/pos - 当前持仓\n"
            "/orders - 最近订单\n"
            "/strategies - 策略列表\n"
            "/run 策略名 - 立即运行本地策略\n"
            "/kill - 🛑 紧急停止全部交易\n"
            "/resume - ✅ 恢复交易"
        )

    async def _cmd_status(self) -> str:
        from app.brokers.manager import get_broker_manager

        db = SessionLocal()
        try:
            day_start = datetime.combine(datetime.now(timezone.utc).date(), time.min,
                                         tzinfo=timezone.utc)
            cfg = db.get(RiskConfig, 1)
            signals = db.scalar(select(func.count(Signal.id))
                                .where(Signal.created_at >= day_start)) or 0
            orders = db.scalar(select(func.count(Order.id))
                               .where(Order.created_at >= day_start)) or 0
            blocked = db.scalar(select(func.count(RiskEventLog.id))
                                .where(RiskEventLog.decision == "block",
                                       RiskEventLog.ts >= day_start)) or 0
        finally:
            db.close()

        manager = get_broker_manager()
        lines = ["📊 系统状态",
                 f"交易开关: {'✅ 允许' if cfg and cfg.trading_enabled else '🛑 已停止'}"]
        for name, info in manager.status().items():
            lines.append(f"{name}: {'在线' if info['connected'] else '离线'}")
            adapter = manager.get_if_connected(name)
            if adapter is not None:
                try:
                    acc = await asyncio.wait_for(adapter.get_account(), timeout=5)
                    lines[-1] += f"（净值 {acc.net_value:,.0f}）"
                except Exception:
                    pass
        lines.append(f"今日: 信号 {signals} / 订单 {orders} / 风控拦截 {blocked}")
        return "\n".join(lines)

    async def _cmd_pos(self) -> str:
        db = SessionLocal()
        try:
            rows = db.scalars(select(Position).where(Position.qty != 0)).all()
        finally:
            db.close()
        if not rows:
            return "当前无持仓"
        lines = ["📋 当前持仓"]
        for p in rows:
            lines.append(f"[{p.broker}] {p.symbol}  {p.qty:g} 股 @ {p.avg_cost:.2f}")
        return "\n".join(lines)

    async def _cmd_orders(self) -> str:
        db = SessionLocal()
        try:
            rows = db.scalars(select(Order).order_by(Order.id.desc()).limit(5)).all()
        finally:
            db.close()
        if not rows:
            return "暂无订单"
        cn = {"buy": "买", "sell": "卖"}
        lines = ["🧾 最近订单"]
        for o in rows:
            price = f" @ {o.avg_fill_price:.2f}" if o.avg_fill_price else ""
            lines.append(f"#{o.id} [{o.broker}] {cn.get(o.side, o.side)} {o.symbol} "
                         f"×{o.qty:g}{price}  {o.status}")
        return "\n".join(lines)

    async def _cmd_strategies(self) -> str:
        db = SessionLocal()
        try:
            rows = db.scalars(select(StrategyConfig).order_by(StrategyConfig.id)).all()
        finally:
            db.close()
        if not rows:
            return "暂无策略配置"
        lines = ["🧠 策略列表"]
        for s in rows:
            mode = "实盘" if s.mode == "live" else "仅提醒"
            state = "✅" if s.enabled else "⏸️"
            local = f"（本地 {s.class_name}）" if s.class_name else "（TV 信号）"
            lines.append(f"{state} {s.name} [{mode}/{s.broker}]{local}")
        return "\n".join(lines)

    async def _cmd_run(self, args: list[str]) -> str:
        if not args:
            return "用法: /run 策略名"
        name = args[0]
        db = SessionLocal()
        try:
            cfg = db.scalar(select(StrategyConfig).where(StrategyConfig.name == name))
        finally:
            db.close()
        if cfg is None:
            return f"策略 {name} 不存在"
        from app.strategy.live import run_strategy_live

        try:
            summary = await run_strategy_live(cfg.id)
        except ValueError as e:
            return f"❌ 运行失败: {e}"
        errs = f"\n异常: {'; '.join(summary['errors'])}" if summary["errors"] else ""
        return f"▶️ {name} 运行完成：{summary['symbols']} 个标的，产生 {summary['signals']} 个信号{errs}"

    async def _cmd_kill_switch(self, enabled: bool) -> str:
        db = SessionLocal()
        try:
            cfg = db.get(RiskConfig, 1)
            cfg.trading_enabled = enabled
            db.commit()
        finally:
            db.close()
        return "✅ 已恢复交易" if enabled else "🛑 已紧急停止全部交易（新信号将被风控拒绝）"


_bot: TelegramBot | None = None


def get_telegram_bot() -> TelegramBot:
    global _bot
    if _bot is None:
        _bot = TelegramBot()
    return _bot
