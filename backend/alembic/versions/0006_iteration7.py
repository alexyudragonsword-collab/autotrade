"""迭代7：账户净值快照表。"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("account_snapshots"):
        op.create_table(
            "account_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("broker", sa.String(32), nullable=False),
            sa.Column("date", sa.String(10), nullable=False),
            sa.Column("cash", sa.Float(), nullable=False, server_default="0"),
            sa.Column("net_value", sa.Float(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("broker", "date", name="uq_account_snapshot_day"),
        )
        op.create_index("ix_account_snapshots_broker", "account_snapshots", ["broker"])


def downgrade() -> None:
    op.drop_table("account_snapshots")
