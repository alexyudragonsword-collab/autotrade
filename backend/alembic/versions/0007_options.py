"""迭代9：期权交易支持。

- signals/orders/positions.symbol: String(32) → String(64)（期权符号更长）
- orders/positions: + multiplier
- risk_config: + 期权五项配置

带存在性/长度检查幂等（0001 基线在新库已按最新元数据建表）。
SQLite 改列需 batch 模式（表重建）；实际上 SQLite 不校验 VARCHAR 长度，
该步骤主要保证 Postgres 部署正确。
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def _has_column(insp, table: str, column: str) -> bool:
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    for table in ("signals", "orders", "positions"):
        cols = {c["name"]: c for c in insp.get_columns(table)}
        length = getattr(cols["symbol"]["type"], "length", None)
        if length is not None and length < 64:
            with op.batch_alter_table(table) as batch:
                batch.alter_column("symbol", type_=sa.String(64),
                                   existing_type=sa.String(length))

    if not _has_column(insp, "orders", "multiplier"):
        op.add_column("orders", sa.Column("multiplier", sa.Float(), nullable=False,
                                          server_default="1"))
    if not _has_column(insp, "positions", "multiplier"):
        op.add_column("positions", sa.Column("multiplier", sa.Float(), nullable=False,
                                             server_default="1"))

    risk_adds = [
        ("options_trading_enabled", sa.Boolean(), sa.text("0")),
        ("allow_naked_selling", sa.Boolean(), sa.text("0")),
        ("max_short_option_notional", sa.Float(), "100000"),
        ("expiry_warn_days", sa.Integer(), "3"),
        ("auto_close_before_expiry", sa.Boolean(), sa.text("0")),
    ]
    for name, type_, default in risk_adds:
        if not _has_column(insp, "risk_config", name):
            op.add_column("risk_config", sa.Column(name, type_, nullable=False,
                                                   server_default=default))


def downgrade() -> None:
    for name in ("options_trading_enabled", "allow_naked_selling",
                 "max_short_option_notional", "expiry_warn_days",
                 "auto_close_before_expiry"):
        op.drop_column("risk_config", name)
    op.drop_column("positions", "multiplier")
    op.drop_column("orders", "multiplier")
