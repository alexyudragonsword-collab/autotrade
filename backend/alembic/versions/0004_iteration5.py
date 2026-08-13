"""迭代5：多账户与持仓守护。

- 新表 broker_accounts
- positions: + high_water_price
- risk_config: + stop_loss_pct / take_profit_pct / trailing_stop_pct

带存在性检查（0001 基线在新库已包含）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _has_column(insp, table: str, column: str) -> bool:
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not insp.has_table("broker_accounts"):
        op.create_table(
            "broker_accounts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(32), nullable=False, unique=True),
            sa.Column("type", sa.String(16), nullable=False),
            sa.Column("params", sa.JSON(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )

    if not _has_column(insp, "positions", "high_water_price"):
        op.add_column("positions", sa.Column("high_water_price", sa.Float(), nullable=True))
    for col in ("stop_loss_pct", "take_profit_pct", "trailing_stop_pct"):
        if not _has_column(insp, "risk_config", col):
            op.add_column("risk_config", sa.Column(col, sa.Float(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_table("broker_accounts")
    op.drop_column("positions", "high_water_price")
    for col in ("stop_loss_pct", "take_profit_pct", "trailing_stop_pct"):
        op.drop_column("risk_config", col)
