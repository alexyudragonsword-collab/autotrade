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
