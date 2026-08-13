from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "dev"
    admin_username: str = "admin"
    admin_password: str = "admin"
    jwt_secret: str = "dev-secret-change-me"
    jwt_expire_minutes: int = 60 * 24
    webhook_secret: str = "dev-webhook-secret"

    database_url: str = "sqlite:///data/autotrade.db"
    data_dir: str = "data"

    tv_ip_allowlist_enabled: bool = False
    # TradingView 官方文档公布的告警出口 IP（可能变化，故默认不启用白名单）
    tv_ip_allowlist: list[str] = [
        "52.89.214.238",
        "34.212.75.30",
        "54.218.53.128",
        "52.32.178.7",
    ]

    paper_enabled: bool = True
    paper_initial_cash: float = 1_000_000.0

    # 港股期权合约乘数兜底值（券商未报告合约规模时使用；US 期权固定 100）
    hk_option_multiplier_default: float = 100.0

    futu_enabled: bool = False
    futu_opend_host: str = "127.0.0.1"
    futu_opend_port: int = 11111
    futu_trd_env: str = "SIMULATE"  # SIMULATE | REAL
    futu_unlock_pwd: str = ""

    ibkr_enabled: bool = False
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497
    ibkr_client_id: int = 17

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_to: str = ""
    wecom_webhook_url: str = ""
    dingtalk_webhook_url: str = ""
    dingtalk_secret: str = ""

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache
def get_settings() -> Settings:
    return Settings()
