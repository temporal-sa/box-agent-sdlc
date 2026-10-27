"""Add immutable storefront cache without changing existing rows."""

from alembic import op

from box_agent_sdlc.db import Asset

revision = "0002"
down_revision = "0001"


def upgrade():
    Asset.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; cached releases must remain readable")
