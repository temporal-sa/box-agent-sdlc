"""Database tests share a server but isolate every test in its own application scope."""

import os
from uuid import uuid4

import pytest
import pytest_asyncio

from box_agent_sdlc.config import Settings
from box_agent_sdlc.db import Database
from box_agent_sdlc.engine import ensure_scope


@pytest.fixture
def settings():
    return Settings(
        database_url=os.getenv("TEST_DATABASE_URL", "postgresql://box:box@localhost:5432/box_sdlc"),
        scope=f"test-{uuid4().hex}",
        mode="scripted",
        auth_mode="token",
        operator_token="test-operator-token",
        webhook_secret="test-webhook-secret",
        github_repo="temporal-sa/box-agent-sdlc-storefront",
        command_actors=("approved-presenter",),
    )


@pytest_asyncio.fixture
async def database(settings):
    db = Database(settings)
    try:
        async with db.session() as session, session.begin():
            await ensure_scope(session, settings.scope)
        yield db
    finally:
        await db.close()
