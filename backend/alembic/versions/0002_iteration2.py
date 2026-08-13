"""迭代2：本地策略驱动、成交盈亏、参数扫描、审计日志。

- strategy_configs: + symbols(JSON), schedule_cron
- trades: + realized_pnl
- backtest_runs: + group_id
- 新表 audit_logs

全部带存在性检查：0001 基线在新库已按最新元数据建表，此处跳过；
旧库（迁移引入前创建）则实际执行 ALTER/CREATE。
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _has_column(insp, table: str, column: str) -> bool:
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not _has_column(insp, "strategy_configs", "symbols"):
        op.add_column("strategy_configs", sa.Column("symbols", sa.JSON(), nullable=True))
        op.execute("UPDATE strategy_configs SET symbols = '[]' WHERE symbols IS NULL")
    if not _has_column(insp, "strategy_configs", "schedule_cron"):
        op.add_column("strategy_configs", sa.Column("schedule_cron", sa.String(64), nullable=True))
    if not _has_column(insp, "trades", "realized_pnl"):
        op.add_column("trades", sa.Column("realized_pnl", sa.Float(), nullable=True))
    if not _has_column(insp, "backtest_runs", "group_id"):
        op.add_column("backtest_runs", sa.Column("group_id", sa.String(32), nullable=True))
        op.create_index("ix_backtest_runs_group_id", "backtest_runs", ["group_id"])

    if not insp.has_table("audit_logs"):
        op.create_table(
            "audit_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("username", sa.String(64), nullable=True),
            sa.Column("method", sa.String(8), nullable=False),
            sa.Column("path", sa.String(200), nullable=False),
            sa.Column("body", sa.Text(), nullable=True),
            sa.Column("status_code", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("ip", sa.String(64), nullable=True),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_audit_logs_ts", "audit_logs", ["ts"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_index("ix_backtest_runs_group_id", "backtest_runs")
    op.drop_column("backtest_runs", "group_id")
    op.drop_column("trades", "realized_pnl")
    op.drop_column("strategy_configs", "schedule_cron")
    op.drop_column("strategy_configs", "symbols")
