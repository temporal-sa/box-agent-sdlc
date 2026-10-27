"""Initial additive durable store. This revision is shared by pipeline v1 and v2."""

from alembic import op

from box_agent_sdlc.db import Base

revision = "0001"
down_revision = None


def upgrade():
    Base.metadata.create_all(op.get_bind())


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore through the retention runbook")
