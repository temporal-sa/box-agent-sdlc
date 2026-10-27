from alembic import context

from box_agent_sdlc.db import Base


def run_migrations():
    connection = context.config.attributes["connection"]
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


run_migrations()
