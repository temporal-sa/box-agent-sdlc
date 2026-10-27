"""Leased, fenced task execution. No database transaction spans an external call."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm.attributes import flag_modified

from .db import Database, Fault, Operation, Run, Scope, Task, Worker, db_now, emit, uid

log = logging.getLogger(__name__)
ACTIVE = {"running", "waiting"}
TERMINAL = {"completed", "failed", "cancelled", "stopped", "rolled_back"}


class LostLease(Exception):
    pass


class AmbiguousEffect(Exception):
    """An external outcome needs intervention; blind retries would be unsafe."""


class BudgetExceeded(Exception):
    pass


class Retryable(Exception):
    def __init__(self, message: str, after: float | None = None):
        super().__init__(message)
        self.after = after


class PermanentError(Exception):
    pass


def obsolete(task, run):
    epoch = task.payload.get("input_epoch")
    return bool(
        epoch
        and epoch != [run.state.get("plan_revision", 1), run.state.get("review_round", 0)]
        and task.payload.get("stage") != "close_old_pr"
    )


@dataclass(frozen=True)
class Claim:
    id: str
    key: str
    scope: str
    run_id: str | None
    kind: str
    payload: dict
    version: str
    fence: int
    attempt: int


async def enqueue(
    session,
    scope: str,
    run_id: str | None,
    key: str,
    kind: str,
    payload: dict | None = None,
    version: str = "any",
    delay: float = 0,
    when=None,
    max_attempts: int = 5,
):
    stamp = await db_now(session)
    await session.execute(
        insert(Task)
        .values(
            id=uid(),
            scope=scope,
            run_id=run_id,
            key=key,
            kind=kind,
            payload=payload or {},
            version=version,
            ready_at=when or stamp + timedelta(seconds=delay),
            status="ready",
            fence=0,
            attempt=0,
            max_attempts=max_attempts,
        )
        .on_conflict_do_nothing(index_elements=[Task.scope, Task.key])
    )


async def ensure_scope(session, scope: str):
    await session.execute(insert(Scope).values(id=scope, version="v1").on_conflict_do_nothing())


class Context:
    def __init__(self, engine: Engine, claim: Claim):
        self.engine, self.db, self.claim = engine, engine.db, claim

    async def assert_lease(self, session):
        task = await session.scalar(select(Task).where(Task.id == self.claim.id).with_for_update())
        stamp = await db_now(session)
        if (
            task is None
            or task.fence != self.claim.fence
            or task.owner != self.engine.owner
            or task.status != "leased"
            or task.lease_until <= stamp
        ):
            raise LostLease(self.claim.id)
        return task

    async def assert_effect_allowed(self, session):
        task = await self.assert_lease(session)
        if not self.claim.run_id:
            return
        run = await session.get(Run, self.claim.run_id)
        rollback = task.payload.get("stage", "").startswith("rollback")
        if (
            run.status in TERMINAL
            or run.status == "paused"
            or obsolete(task, run)
            or (run.state.get("compensating") and not rollback)
        ):
            raise LostLease("No new effect may start for cancelled or superseded work")

    async def effect(
        self,
        name: str,
        request: dict,
        execute: Callable[[], Awaitable[Any]],
        reconcile: Callable[[], Awaitable[Any | None]] | None = None,
        *,
        repeat_safe: bool = False,
        model: bool = False,
        reservation_usd: float | None = None,
    ) -> Any:
        """Persist intent before I/O and result afterward.

        Mutations must provide a reconciliation function. Model calls can repeat, but an
        abandoned reservation stays charged as uncertain spend rather than silently disappearing.
        """
        op_id = f"{self.claim.scope}/{self.claim.key}/{name}"
        if len(op_id) > 380:
            op_id = f"{self.claim.scope}/sha256-{hashlib.sha256(op_id.encode()).hexdigest()}"
        digest = hashlib.sha256(json.dumps(request, sort_keys=True, default=str).encode()).hexdigest()
        recover = False
        async with self.db.session() as s, s.begin():
            await self.assert_effect_allowed(s)
            op = await s.get(Operation, op_id, with_for_update=True)
            if op:
                if op.request_hash != digest:
                    raise PermanentError("Stable operation ID reused with different input")
                if op.status == "completed":
                    return deepcopy(op.result["value"])
                recover = True
                op.fence = self.claim.fence
                op.attempts += 1
            else:
                op = Operation(
                    id=op_id,
                    task_id=self.claim.id,
                    run_id=self.claim.run_id,
                    request_hash=digest,
                    fence=self.claim.fence,
                )
                s.add(op)
            if model and self.claim.run_id:
                run = await s.get(Run, self.claim.run_id, with_for_update=True)
                amount = Decimal(str(max(self.engine.settings.model_reservation_usd, reservation_usd or 0)))
                if recover and op.reservation:
                    # Unknown previous provider outcome: retain its upper-bound allowance.
                    run.reserved -= op.reservation
                    run.spent += op.reservation
                    await emit(
                        s,
                        run.scope,
                        run.id,
                        "uncertain_model_spend",
                        amount=float(op.reservation),
                        operation=op_id,
                    )
                    op.reservation = Decimal("0")
                if run.spent + run.reserved + amount > run.budget:
                    raise BudgetExceeded("Insufficient unreserved budget for another model request")
                run.reserved += amount
                op.reservation = amount
            await emit(
                s,
                self.claim.scope,
                self.claim.run_id,
                "operation_started",
                operation=op_id,
                recovery=recover,
                attempt=self.claim.attempt,
            )
        # Cancellation may have committed while intent was being persisted. This
        # check stops new calls after observing it; calls already issued still
        # need external reconciliation and cannot be recalled atomically.
        async with self.db.session() as s, s.begin():
            await self.assert_effect_allowed(s)
        if recover and reconcile:
            value = await reconcile()
            if value is not None:
                return await self._record(op_id, value, model)
            if not repeat_safe:
                async with self.db.session() as s, s.begin():
                    await self.assert_lease(s)
                    op = await s.get(Operation, op_id, with_for_update=True)
                    op.status = "ambiguous"
                raise AmbiguousEffect(
                    f"Reconciliation cannot establish the outcome for {name}; no repeated mutation was issued"
                )
        elif recover and not repeat_safe:
            async with self.db.session() as s, s.begin():
                await self.assert_lease(s)
                op = await s.get(Operation, op_id, with_for_update=True)
                op.status = "ambiguous"
            raise AmbiguousEffect(f"Cannot establish external outcome for {name}")
        value = await execute()
        await self.fault("after_effect")
        return await self._record(op_id, value, model)

    async def _record(self, op_id: str, value: Any, model: bool):
        # Fail before serializing a non-durable provider object.
        value = json.loads(json.dumps(value, default=str))
        async with self.db.session() as s, s.begin():
            await self.assert_lease(s)
            op = await s.get(Operation, op_id, with_for_update=True)
            if op.fence != self.claim.fence:
                raise LostLease(op_id)
            op.status, op.result = "completed", {"value": value}
            if model and self.claim.run_id:
                run = await s.get(Run, self.claim.run_id, with_for_update=True)
                usage = value.get("usage", {}) if isinstance(value, dict) else {}
                cost = usage.get("cost_usd")
                run.reserved -= op.reservation
                if cost is None:
                    run.spent += op.reservation
                    await emit(
                        s,
                        run.scope,
                        run.id,
                        "unknown_model_price",
                        operation=op_id,
                        allowance=float(op.reservation),
                    )
                else:
                    run.spent += Decimal(str(cost))
                op.reservation = Decimal("0")
            await emit(s, self.claim.scope, self.claim.run_id, "operation_completed", operation=op_id)
        return value

    async def fault(self, point: str):
        if not self.claim.run_id:
            return
        fault_kind = None
        async with self.db.session() as s, s.begin():
            await self.assert_lease(s)
            fault = await s.scalar(
                select(Fault)
                .where(Fault.run_id == self.claim.run_id, Fault.point == point, Fault.consumed_at.is_(None))
                .order_by(Fault.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if fault:
                fault.consumed_at = await db_now(s)
                fault_kind = fault.kind
                await emit(
                    s,
                    self.claim.scope,
                    self.claim.run_id,
                    "fault_consumed",
                    fault_id=fault.id,
                    kind=fault.kind,
                    point=point,
                )
        if fault_kind == "worker_exit":
            os._exit(75)
        if fault_kind == "provider_429":
            raise Retryable("Injected provider rate limit", after=2)
        if fault_kind == "executor_failure":
            raise Retryable("Injected executor failure", after=2)
        return fault_kind


class Engine:
    def __init__(self, db: Database, handler, advance, *, owner: str | None = None, on_failure=None):
        self.db, self.settings = db, db.settings
        self.handler, self.advance = handler, advance
        self.on_failure = on_failure or getattr(getattr(advance, "__self__", None), "on_failure", None)
        self.owner = owner or f"{socket.gethostname()}-{uid()[:10]}"
        self.stopping = asyncio.Event()

    async def claim(self) -> Claim | None:
        async with self.db.session() as s, s.begin():
            stamp = await db_now(s)
            # Lease reclamation itself is atomic; an old owner's fence becomes invalid.
            task = await s.scalar(
                select(Task)
                .where(
                    Task.scope == self.settings.scope,
                    Task.version.in_(["any", self.settings.version]),
                    or_(
                        and_(Task.status == "ready", Task.ready_at <= stamp),
                        and_(Task.status == "leased", Task.lease_until <= stamp),
                    ),
                )
                .order_by(Task.ready_at, Task.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not task:
                return None
            if task.run_id:
                run = await s.get(Run, task.run_id)
                if run.status in TERMINAL:
                    task.status = "cancelled"
                    return None
                if obsolete(task, run):
                    task.status = "cancelled"
                    return None
                if (
                    run.state.get("compensating")
                    and task.kind != "command"
                    and not task.payload.get("stage", "").startswith("rollback")
                ):
                    task.status = "cancelled"
                    return None
                if run.status == "paused" and task.kind != "command":
                    task.status = "blocked"
                    return None
            if task.attempt >= task.max_attempts:
                task.status = "failed"
                if task.run_id:
                    run.status = "failed"
                    state = deepcopy(run.state)
                    state["failure"] = task.error or "Task lease expired too many times"
                    run.state = state
                    if self.on_failure and not state.get("compensating"):
                        await self.on_failure(s, run, state["failure"])
                    flag_modified(run, "state")
                await emit(s, task.scope, task.run_id, "retry_exhausted", task=task.key)
                return None
            recovered = task.status == "leased"
            task.owner, task.fence = self.owner, task.fence + 1
            task.attempt += 1
            task.status, task.lease_until = "leased", stamp + timedelta(seconds=self.settings.lease_seconds)
            await emit(
                s,
                task.scope,
                task.run_id,
                "task_claimed",
                task=task.key,
                attempt=task.attempt,
                recovered=recovered,
                worker=self.owner,
                fence=task.fence,
            )
            return Claim(
                task.id,
                task.key,
                task.scope,
                task.run_id,
                task.kind,
                deepcopy(task.payload),
                task.version,
                task.fence,
                task.attempt,
            )

    async def renew(self, claim: Claim):
        while True:
            await asyncio.sleep(self.settings.heartbeat_seconds)
            async with self.db.session() as s, s.begin():
                ctx = Context(self, claim)
                task = await ctx.assert_lease(s)
                task.lease_until = await db_now(s) + timedelta(seconds=self.settings.lease_seconds)

    async def execute(self, claim: Claim):
        ctx = Context(self, claim)
        renewal = asyncio.create_task(self.renew(claim))
        work = None
        try:
            await ctx.fault("before_task")
            async with self.db.session() as s:
                run = await s.get(Run, claim.run_id) if claim.run_id else None
            work = asyncio.create_task(self.handler(ctx, run, claim.payload))
            done, _ = await asyncio.wait([work, renewal], return_when=asyncio.FIRST_COMPLETED)
            if renewal in done:
                await renewal  # propagate loss of lease / database connectivity
            result = await work
            async with self.db.session() as s, s.begin():
                task = await ctx.assert_lease(s)
                run = await s.get(Run, claim.run_id, with_for_update=True) if claim.run_id else None
                if run and run.status in TERMINAL:
                    task.status = "cancelled"
                    return
                if run and obsolete(task, run):
                    task.status = "cancelled"
                    await emit(s, claim.scope, claim.run_id, "obsolete_completion_discarded", task=claim.key)
                    return
                if (
                    run
                    and run.state.get("compensating")
                    and claim.kind != "command"
                    and not claim.payload.get("stage", "").startswith("rollback")
                ):
                    task.status = "cancelled"
                    return
                task.status, task.result = "completed", result
                task.lease_until = None
                if run:
                    run.updated_at = await db_now(s)
                    run.revision += 1
                await self.advance(s, task, run, result)
                if run:
                    flag_modified(run, "state")
                await emit(
                    s, claim.scope, claim.run_id, "task_completed", task=claim.key, attempt=claim.attempt
                )
        except LostLease:
            log.info("Discarding stale completion: %s", claim.key)
        except (BudgetExceeded, AmbiguousEffect) as exc:
            await self.fail(claim, exc, paused=True)
        except Exception as exc:
            log.exception("Task failed: %s", claim.key)
            await self.fail(claim, exc)
        finally:
            renewal.cancel()
            if work and not work.done():
                work.cancel()
            await asyncio.gather(renewal, *([work] if work else []), return_exceptions=True)

    async def fail(self, claim: Claim, exc: Exception, paused=False):
        try:
            async with self.db.session() as s, s.begin():
                task = await Context(self, claim).assert_lease(s)
                task.error = f"{type(exc).__name__}: {str(exc)[:1500]}"
                permanent = isinstance(exc, PermanentError)
                task.status = (
                    "blocked"
                    if paused
                    else "failed"
                    if permanent or task.attempt >= task.max_attempts
                    else "ready"
                )
                delay = getattr(exc, "after", None)
                task.ready_at = await db_now(s) + timedelta(
                    seconds=delay if delay is not None else min(60, 2**task.attempt)
                )
                task.lease_until = None
                if claim.run_id:
                    run = await s.get(Run, claim.run_id, with_for_update=True)
                    state = deepcopy(run.state)
                    if paused:
                        run.status = "paused"
                        state["gate"] = {
                            "key": "budget" if isinstance(exc, BudgetExceeded) else "intervention",
                            "question": str(exc),
                            "commands": ["/continue", "/stop"]
                            if isinstance(exc, BudgetExceeded)
                            else ["/stop"],
                            "waiting_since": (await db_now(s)).timestamp(),
                        }
                    elif task.status == "failed":
                        run.status = "failed"
                        state["failure"] = task.error
                    run.state = state
                    if task.status == "failed" and self.on_failure and not state.get("compensating"):
                        await self.on_failure(s, run, task.error)
                    flag_modified(run, "state")
                await emit(
                    s,
                    claim.scope,
                    claim.run_id,
                    "task_error",
                    task=claim.key,
                    error=task.error,
                    retry_at=task.ready_at.isoformat(),
                    paused=paused,
                )
        except LostLease:
            pass

    async def heartbeat(self):
        async with self.db.session() as s, s.begin():
            stamp = await db_now(s)
            await s.execute(
                insert(Worker)
                .values(
                    id=self.owner, scope=self.settings.scope, version=self.settings.version, heartbeat=stamp
                )
                .on_conflict_do_update(index_elements=[Worker.id], set_={"heartbeat": stamp})
            )
            # Run expiry is independent of an idle gate and survives all runner restarts.
            expired = list(
                await s.scalars(
                    select(Run)
                    .where(
                        Run.scope == self.settings.scope,
                        Run.status.in_(ACTIVE | {"paused"}),
                        Run.deadline <= stamp,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for run in expired:
                if run.state.get("compensating"):
                    continue
                run.status = "failed"
                run.state = {**run.state, "failure": "Three-day execution deadline exceeded", "gate": None}
                if self.on_failure:
                    await self.on_failure(s, run, "Three-day execution deadline exceeded")
                flag_modified(run, "state")
                await emit(s, run.scope, run.id, "run_expired")

    async def serve(self, concurrency=4):
        active: set[asyncio.Task] = set()
        while not self.stopping.is_set():
            try:
                await self.heartbeat()
                active = {x for x in active if not x.done()}
                while len(active) < concurrency:
                    claim = await self.claim()
                    if not claim:
                        break
                    active.add(asyncio.create_task(self.execute(claim)))
            except Exception:
                log.exception("Runner database unavailable; no new external work will start")
            try:
                await asyncio.wait_for(self.stopping.wait(), self.settings.poll_seconds)
            except TimeoutError:
                pass
        if active:
            await asyncio.gather(*active, return_exceptions=True)
