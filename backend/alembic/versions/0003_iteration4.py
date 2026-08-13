"""迭代4：多周期支持。

- strategy_configs: + timeframe（默认 1d）
- backtest_runs: + timeframe（默认 1d）

带存在性检查（0001 基线在新库已含这些列）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _has_column(insp, table: str, column: str) -> bool:
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_column(insp, "strategy_configs", "timeframe"):
        op.add_column("strategy_configs",
                      sa.Column("timeframe", sa.String(8), nullable=False, server_default="1d"))
    if not _has_column(insp, "backtest_runs", "timeframe"):
        op.add_column("backtest_runs",
                      sa.Column("timeframe", sa.String(8), nullable=False, server_default="1d"))


def downgrade() -> None:
    op.drop_column("backtest_runs", "timeframe")
    op.drop_column("strategy_configs", "timeframe")
