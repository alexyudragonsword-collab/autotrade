import os

# 必须在导入 app 之前设置：测试用独立 SQLite 与固定密钥
os.environ.setdefault("DATABASE_URL", "sqlite:///data/test_autotrade.db")
os.environ.setdefault("DATA_DIR", "data/test_data")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")

from pathlib import Path

import pytest

from app.bootstrap import seed_all
from app.db.base import Base, SessionLocal, engine, init_db


@pytest.fixture(autouse=True)
def clean_db():
    """每个测试用干净的数据库。"""
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)
    # alembic_version 不在 ORM 元数据里，需一并清掉，否则下个测试 init_db 会跳过建表
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


@pytest.fixture(autouse=True)
def _offline_market_data(monkeypatch):
    """测试一律不联网取行情：BarStore 增量刷新拿到的永远是空数据，只用测试自己写入的缓存。

    否则在能联网的机器上，测试代码（如 US.UP）若恰好是真实股票，真实行情会混进测试数据。
    """
    import app.data.store as store_mod
    from app.data.base import DataProvider, empty_bars

    class _OfflineProvider(DataProvider):
        def get_bars(self, symbol, start, end, interval="1d"):
            return empty_bars()

        def get_universe(self, market, universe):
            return []

    monkeypatch.setattr(store_mod, "get_provider", lambda market: _OfflineProvider())


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def seeded(db):
    seed_all(db)
    return db


def pytest_sessionfinish(session, exitstatus):
    for f in Path("data").glob("test_autotrade.db*"):
        try:
            f.unlink()
        except OSError:
            pass
