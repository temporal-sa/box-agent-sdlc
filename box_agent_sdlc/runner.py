"""Each registry runner serves a pinned pipeline version and shares the PostgreSQL queue."""

import asyncio
import logging
import os
import signal

from .config import Settings
from .db import Database
from .engine import Engine
from .pipeline import Pipeline


async def main():
    settings = Settings()
    settings.validate()
    db = Database(settings)
    github = workspace = None
    if settings.mode == "live":
        if not settings.github_token or not settings.executor_token:
            raise ValueError("Live runner requires GITHUB_TOKEN and EXECUTOR_TOKEN")
        from .integrations import GitHubClient, GitWorkspaceManager

        github = GitHubClient(token=settings.github_token)
        workspace = GitWorkspaceManager(
            os.getenv("WORKDIR_ROOT", "/tmp/box-workspaces"),
            token=settings.github_token,
            executor_url=settings.executor_url,
            executor_token=settings.executor_token,
        )
    pipeline = Pipeline(db, github=github, workspace=workspace)
    engine = Engine(db, pipeline.handle, pipeline.advance)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, engine.stopping.set)
    try:
        await engine.serve(concurrency=int(os.getenv("RUNNER_CONCURRENCY", "4")))
    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
