"""${message}"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# 修订关系固定在脚本中，迁移不得依赖运行时业务配置。
revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}

def upgrade() -> None:
    """Apply this revision."""
    ${upgrades if upgrades else "pass"}

def downgrade() -> None:
    """Reverse this revision when data semantics allow it."""
    ${downgrades if downgrades else "pass"}
