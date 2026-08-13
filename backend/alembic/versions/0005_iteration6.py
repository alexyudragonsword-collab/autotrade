"""迭代6：自定义策略表。"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("custom_strategies"):
        op.create_table(
            "custom_strategies",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("class_name", sa.String(64), nullable=False, unique=True),
            sa.Column("code", sa.Text(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("custom_strategies")
