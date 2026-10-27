"""PostgreSQL is the authoritative queue, inbox, state store, and event history."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import Settings


def now() -> datetime:
    return datetime.now(UTC)


def uid() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Scope(Base):
    __tablename__ = "scopes"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    version: Mapped[str] = mapped_column(default="v1")


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    scope: Mapped[str] = mapped_column(ForeignKey("scopes.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    version: Mapped[str] = mapped_column(String(20))
    mode: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), default="running", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    budget: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("3"))
    spent: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    reserved: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    scope: Mapped[str] = mapped_column(String(100), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), index=True)
    key: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(40))
    version: Mapped[str] = mapped_column(String(20), default="any")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="ready")
    ready_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner: Mapped[str | None] = mapped_column(String(100))
    fence: Mapped[int] = mapped_column(Integer, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(String(2000))
    __table_args__ = (
        UniqueConstraint("scope", "key"),
        Index("tasks_due", "scope", "version", "status", "ready_at"),
    )


class Operation(Base):
    __tablename__ = "operations"
    id: Mapped[str] = mapped_column(String(400), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), index=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="started")
    fence: Mapped[int] = mapped_column(Integer)
    result: Mapped[dict | None] = mapped_column(JSONB)
    reservation: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Inbox(Base):
    __tablename__ = "inbox"
    id: Mapped[str] = mapped_column(String(300), primary_key=True)
    scope: Mapped[str] = mapped_column(String(100))
    event: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(JSONB)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Comment(Base):
    __tablename__ = "comments"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(200))
    author: Mapped[str] = mapped_column(String(100))
    body: Mapped[str] = mapped_column(String(8000))
    authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    handled: Mapped[bool] = mapped_column(Boolean, default=False)
    url: Mapped[str] = mapped_column(default="")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("run_id", "external_id"),)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(100), index=True)
    run_id: Mapped[str | None] = mapped_column(String(200), index=True)
    kind: Mapped[str] = mapped_column(String(80))
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Worker(Base):
    __tablename__ = "workers"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    scope: Mapped[str] = mapped_column(String(100), index=True)
    version: Mapped[str] = mapped_column(String(20))
    heartbeat: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Fault(Base):
    __tablename__ = "faults"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    point: Mapped[str] = mapped_column(String(100), default="before_task")
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(String(100))


class Environment(Base):
    __tablename__ = "environments"
    scope: Mapped[str] = mapped_column(String(100), primary_key=True)
    owner: Mapped[str | None] = mapped_column(String(200))
    generation: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[dict] = mapped_column(JSONB, default=dict)


class Asset(Base):
    """Bounded immutable storefront cache; web needs no private-repository credentials."""

    __tablename__ = "assets"
    scope: Mapped[str] = mapped_column(String(100), primary_key=True)
    sha: Mapped[str] = mapped_column(String(40), primary_key=True)
    path: Mapped[str] = mapped_column(String(300), primary_key=True)
    content: Mapped[str] = mapped_column()


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.engine = create_async_engine(
            settings.sqlalchemy_url, pool_pre_ping=True, pool_size=5, max_overflow=5
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> AsyncSession:
        return self.sessions()

    async def close(self) -> None:
        await self.engine.dispose()


async def emit(session: AsyncSession, scope: str, run_id: str | None, event_type: str, **data) -> None:
    session.add(Event(scope=scope, run_id=run_id, kind=event_type, data=data))


async def db_now(session: AsyncSession) -> datetime:
    return await session.scalar(func.clock_timestamp())
