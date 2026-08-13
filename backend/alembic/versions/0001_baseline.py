"""基线：按当前 ORM 元数据建全部表。

新库直接得到完整最新 schema；后续迁移（如 0002）对已存在的
列/表做存在性检查后跳过，保证两条路径都幂等。
"""

from alembic import op

from app.db.base import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
