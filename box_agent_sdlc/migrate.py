"""Serialize additive schema upgrades across overlapping application revisions."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from .config import Settings


def main():
    engine = create_engine(Settings().sqlalchemy_url)
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(702817401)"))
        connection.commit()
        try:
            config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.commit()
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(702817401)"))
            connection.commit()
    engine.dispose()


if __name__ == "__main__":
    main()
