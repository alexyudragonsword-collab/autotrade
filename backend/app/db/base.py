from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine():
    settings = get_settings()
    url = settings.database_url
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        # sqlite:///data/autotrade.db → 确保目录存在
        db_path = url.split("///", 1)[-1]
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False}
    eng = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _set_sqlite_pragma(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    return eng


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """通过 Alembic 迁移建/升级 schema。

    - 新库：0001 基线按最新元数据建全表，后续迁移幂等跳过
    - 迁移机制引入前创建的旧库（有业务表但无 alembic_version）：先 stamp 到 0001 再升级
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    from app.db import models  # noqa: F401  确保模型已注册

    backend_dir = Path(__file__).resolve().parents[2]
    cfg = Config(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    cfg.attributes["connection"] = None  # 让 env.py 自建连接

    insp = inspect(engine)
    if insp.has_table("signals") and not insp.has_table("alembic_version"):
        command.stamp(cfg, "0001")
    command.upgrade(cfg, "head")
