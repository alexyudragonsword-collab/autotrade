import asyncio
import smtplib
from email.mime.text import MIMEText

from app.config import get_settings
from app.domain.schemas import NotifyEvent
from app.notify.base import Notifier


class EmailNotifier(Notifier):
    type = "email"

    async def send(self, event: NotifyEvent) -> None:
        await asyncio.to_thread(self._send_sync, event)

    def _send_sync(self, event: NotifyEvent) -> None:
        s = get_settings()
        if not s.smtp_host or not s.smtp_to:
            raise RuntimeError("未配置 SMTP_HOST / SMTP_TO")
        msg = MIMEText(self.format_text(event), "plain", "utf-8")
        msg["Subject"] = f"[AutoTrade] {event.title}"
        msg["From"] = s.smtp_user
        msg["To"] = s.smtp_to
        cls = smtplib.SMTP_SSL if s.smtp_port == 465 else smtplib.SMTP
        with cls(s.smtp_host, s.smtp_port, timeout=15) as server:
            if s.smtp_port != 465:
                server.starttls()
            if s.smtp_user:
                server.login(s.smtp_user, s.smtp_password)
            server.sendmail(s.smtp_user, s.smtp_to.split(","), msg.as_string())
