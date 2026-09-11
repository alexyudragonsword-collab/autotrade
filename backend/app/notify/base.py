from abc import ABC, abstractmethod

from app.domain.schemas import NotifyEvent


class Notifier(ABC):
    type: str = ""

    @abstractmethod
    async def send(self, event: NotifyEvent) -> None: ...

    @staticmethod
    def format_text(event: NotifyEvent) -> str:
        lines = [f"【{event.title}】", event.body]
        for k, v in event.fields.items():
            lines.append(f"{k}: {v}")
        return "\n".join(x for x in lines if x)

    @staticmethod
    def format_body(event: NotifyEvent) -> str:
        """正文（不含标题）—— 供标题/正文分离的渠道（Bark、Server酱）使用。"""
        lines = [event.body]
        for k, v in event.fields.items():
            lines.append(f"{k}: {v}")
        return "\n".join(x for x in lines if x)
