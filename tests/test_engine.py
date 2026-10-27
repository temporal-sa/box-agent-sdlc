"""Database-backed failure-boundary tests; each test owns a distinct application scope."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from box_agent_sdlc.config import Settings
from box_agent_sdlc.db import Comment, Database, Event, Operation, Run, Task, db_now
from box_agent_sdlc.engine import Context, Engine, PermanentError, Retryable, enqueue, ensure_scope
from box_agent_sdlc.pipeline import Pipeline


@pytest.fixture
async def engine_db():
    settings = Settings(
        database_url=os.environ.get("TEST_DATABASE_URL", "postgresql://box:box@localhost:5432/box_sdlc"),
        scope=f"engine-test-{uuid.uuid4().hex}",
        auth_mode="development",
        mode="scripted",
        lease_seconds=30,
        heartbeat_seconds=5,
    )
    db = Database(settings)
    async with db.session() as session, session.begin():
        await ensure_scope(session, settings.scope)
    yield db
    await db.close()


async def seed(
    db, *, key="work", kind="stage", budget="3", status="running", payload=None, version="v1", max_attempts=5
):
    run_id = f"{db.settings.scope}.{uuid.uuid4().hex[:8]}"
    async with db.session() as session, session.begin():
        stamp = await db_now(session)
        run = Run(
            id=run_id,
            scope=db.settings.scope,
            kind="issue",
            version=version,
            mode="scripted",
            status=status,
            budget=Decimal(budget),
            deadline=stamp + timedelta(days=1),
            state={
                "title": "Durability boundary test",
                "gate": None,
                "conversation": [],
                "steps": [],
                "usage": {},
                "plan_revision": 1,
            },
        )
        session.add(run)
        await session.flush()
        await enqueue(
            session, db.settings.scope, run_id, key, kind, payload or {}, version, max_attempts=max_attempts
        )
    return run_id


async def no_advance(session, task, run, result):
    return None


async def success(ctx, run, payload):
    return {"value": "finished"}


def runner(db, handler=success, advance=no_advance, owner="worker-a"):
    return Engine(db, handler, advance, owner=f"{db.settings.scope}:{owner}")


async def rows(db, run_id):
    async with db.session() as session:
        run = await session.get(Run, run_id)
        tasks = list(await session.scalars(select(Task).where(Task.run_id == run_id).order_by(Task.key)))
        return run, tasks


async def expire(db, task_id):
    async with db.session() as session, session.begin():
        await session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(lease_until=await db_now(session) - timedelta(seconds=1))
        )


async def seed_operation(db, claim, name, request, *, reservation="0"):
    op_id = f"{claim.scope}/{claim.key}/{name}"
    digest = hashlib.sha256(json.dumps(request, sort_keys=True, default=str).encode()).hexdigest()
    async with db.session() as session, session.begin():
        session.add(
            Operation(
                id=op_id,
                task_id=claim.id,
                run_id=claim.run_id,
                request_hash=digest,
                fence=claim.fence,
                reservation=Decimal(reservation),
            )
        )
    return op_id


async def test_competing_workers_claim_a_task_once(engine_db):
    run_id = await seed(engine_db)
    first, second = await asyncio.gather(
        runner(engine_db, owner="a").claim(), runner(engine_db, owner="b").claim()
    )
    claims = [claim for claim in (first, second) if claim]
    assert len(claims) == 1
    assert claims[0].attempt == 1
    _, tasks = await rows(engine_db, run_id)
    assert tasks[0].status == "leased"
    assert tasks[0].fence == 1


async def test_duplicate_enqueue_and_version_filter(engine_db):
    run_id = await seed(engine_db, version="v2")
    async with engine_db.session() as session, session.begin():
        await enqueue(session, engine_db.settings.scope, run_id, "work", "stage", version="v2")
    assert await runner(engine_db).claim() is None
    _, tasks = await rows(engine_db, run_id)
    assert len(tasks) == 1
    assert tasks[0].status == "ready"


async def test_stale_completion_cannot_overwrite_new_owner(engine_db):
    run_id = await seed(engine_db)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(ctx, run, payload):
        entered.set()
        await release.wait()
        return {"value": "stale"}

    old = runner(engine_db, delayed, owner="old")
    old_claim = await old.claim()
    assert old_claim
    work = asyncio.create_task(old.execute(old_claim))
    await asyncio.wait_for(entered.wait(), timeout=5)
    await expire(engine_db, old_claim.id)
    new = runner(engine_db, owner="new")
    new_claim = await new.claim()
    assert new_claim and new_claim.id == old_claim.id
    assert new_claim.fence == old_claim.fence + 1
    assert new_claim.attempt == 2
    release.set()
    await asyncio.wait_for(work, timeout=5)
    run, tasks = await rows(engine_db, run_id)
    assert tasks[0].status == "leased"
    assert tasks[0].owner == new.owner
    assert tasks[0].result is None
    assert run.revision == 0
    await new.execute(new_claim)
    run, tasks = await rows(engine_db, run_id)
    assert tasks[0].result == {"value": "finished"}
    assert tasks[0].status == "completed"
    assert run.revision == 1


async def test_checkpoint_reuse_skips_completed_external_call(engine_db):
    await seed(engine_db)
    engine = runner(engine_db)
    claim = await engine.claim()
    assert claim
    context = Context(engine, claim)
    calls = 0

    async def effect():
        nonlocal calls
        calls += 1
        return {"sha": "saved-result"}

    first = await context.effect("commit", {"branch": "stable-name"}, effect)
    second = await context.effect("commit", {"branch": "stable-name"}, effect)
    assert first == second == {"sha": "saved-result"}
    assert calls == 1
    with pytest.raises(PermanentError, match="different input"):
        await context.effect("commit", {"branch": "different-name"}, effect)
    assert calls == 1


async def test_uncertain_mutation_pauses_without_blind_retry(engine_db):
    run_id = await seed(engine_db)
    calls = 0

    async def handler(context, run, payload):
        async def mutate():
            nonlocal calls
            calls += 1
            return {"pr": 22}

        return await context.effect("open-pr", {"branch": "run-specific"}, mutate)

    engine = runner(engine_db, handler)
    claim = await engine.claim()
    assert claim
    op_id = await seed_operation(engine_db, claim, "open-pr", {"branch": "run-specific"})
    await engine.execute(claim)
    run, tasks = await rows(engine_db, run_id)
    assert calls == 0
    assert run.status == "paused"
    assert run.state["gate"]["key"] == "intervention"
    assert tasks[0].status == "blocked"
    async with engine_db.session() as session:
        assert (await session.get(Operation, op_id)).status == "ambiguous"


async def test_reconciliation_recovers_remote_result_without_second_mutation(engine_db):
    await seed(engine_db)
    engine = runner(engine_db)
    claim = await engine.claim()
    assert claim
    context = Context(engine, claim)
    await seed_operation(engine_db, claim, "open-pr", {"branch": "run-specific"})
    calls = []

    async def mutate():
        calls.append("mutate")
        return {"pr": 23}

    async def reconcile():
        calls.append("lookup")
        return {"pr": 22}

    result = await context.effect("open-pr", {"branch": "run-specific"}, mutate, reconcile)
    assert result == {"pr": 22}
    assert calls == ["lookup"]
    assert await context.effect("open-pr", {"branch": "run-specific"}, mutate, reconcile) == result
    assert calls == ["lookup"]


async def test_reconciliation_absence_cannot_authorize_reposting_an_uncertain_mutation(engine_db):
    run_id = await seed(engine_db)
    calls = []

    async def handler(context, run, payload):
        async def mutate():
            calls.append("post")
            return {"issue": 22}

        async def reconcile():
            calls.append("lookup")
            # A recently accepted GitHub mutation may not appear in collection reads yet.

        return await context.effect("open-issue", {"title": "stable operation"}, mutate, reconcile)

    engine = runner(engine_db, handler)
    claim = await engine.claim()
    assert claim
    op_id = await seed_operation(engine_db, claim, "open-issue", {"title": "stable operation"})
    await engine.execute(claim)
    run, tasks = await rows(engine_db, run_id)
    assert calls == ["lookup"]
    assert run.status == "paused"
    assert run.state["gate"]["key"] == "intervention"
    assert tasks[0].status == "blocked"
    async with engine_db.session() as session:
        assert (await session.get(Operation, op_id)).status == "ambiguous"


async def test_model_budget_blocks_call_before_provider_spend(engine_db):
    run_id = await seed(engine_db, budget="0.1")
    calls = 0

    async def handler(context, run, payload):
        async def provider():
            nonlocal calls
            calls += 1
            return {"usage": {"cost_usd": 0.05}}

        return await context.effect("model", {"prompt": "test"}, provider, repeat_safe=True, model=True)

    engine = runner(engine_db, handler)
    claim = await engine.claim()
    assert claim
    await engine.execute(claim)
    run, tasks = await rows(engine_db, run_id)
    assert calls == 0
    assert run.status == "paused"
    assert run.state["gate"]["key"] == "budget"
    assert tasks[0].status == "blocked"
    assert run.spent == run.reserved == 0


async def test_abandoned_model_reservation_stays_charged_on_recovery(engine_db):
    run_id = await seed(engine_db, budget="1")
    engine = runner(engine_db)
    old = await engine.claim()
    assert old
    await seed_operation(engine_db, old, "model", {"prompt": "test"}, reservation="0.25")
    async with engine_db.session() as session, session.begin():
        run = await session.get(Run, run_id, with_for_update=True)
        run.reserved = Decimal("0.25")
    await expire(engine_db, old.id)
    claim = await runner(engine_db, owner="recovery").claim()
    assert claim
    recovery = runner(engine_db, owner="recovery")

    async def provider():
        return {"usage": {"cost_usd": 0.05}, "answer": "finished"}

    result = await Context(recovery, claim).effect(
        "model", {"prompt": "test"}, provider, repeat_safe=True, model=True
    )
    assert result["answer"] == "finished"
    run, _ = await rows(engine_db, run_id)
    assert run.spent == Decimal("0.30")
    assert run.reserved == 0
    async with engine_db.session() as session:
        events = list(await session.scalars(select(Event).where(Event.run_id == run_id)))
    assert any(event.kind == "uncertain_model_spend" for event in events)


async def test_continue_command_can_run_while_paused_and_unblocks_work(engine_db):
    run_id = await seed(engine_db, status="paused", budget="0.1")
    async with engine_db.session() as session, session.begin():
        run = await session.get(Run, run_id, with_for_update=True)
        run.state = {**run.state, "gate": {"key": "budget", "commands": ["/continue", "/stop"]}}
        await session.execute(update(Task).where(Task.run_id == run_id).values(status="blocked"))
        comment = Comment(
            run_id=run_id,
            external_id="operator-continue",
            author="operator",
            body="/continue",
            authorized=True,
        )
        session.add(comment)
        await session.flush()
        await enqueue(
            session,
            engine_db.settings.scope,
            run_id,
            "continue",
            "command",
            {"comment_id": comment.id},
            version="v1",
        )
    pipeline = Pipeline(engine_db)
    engine = runner(engine_db, pipeline.handle, pipeline.advance)
    claim = await engine.claim()
    assert claim and claim.kind == "command"
    await engine.execute(claim)
    run, tasks = await rows(engine_db, run_id)
    assert run.status == "running"
    assert run.state["gate"] is None
    assert run.budget == Decimal("3.1")
    assert next(task for task in tasks if task.key == "work").status == "ready"
    next_claim = await engine.claim()
    assert next_claim and next_claim.key == "work"


async def test_retry_exhaustion_is_finite_and_visible(engine_db):
    run_id = await seed(engine_db, max_attempts=2)

    async def failing(context, run, payload):
        raise Retryable("provider unavailable", after=0)

    engine = runner(engine_db, failing)
    for attempt in (1, 2):
        claim = await engine.claim()
        assert claim and claim.attempt == attempt
        await engine.execute(claim)
    run, tasks = await rows(engine_db, run_id)
    assert tasks[0].status == "failed"
    assert tasks[0].attempt == 2
    assert run.status == "failed"
    assert "provider unavailable" in run.state["failure"]
    assert await engine.claim() is None


async def test_expired_run_fails_without_restarting_a_waiting_gate(engine_db):
    run_id = await seed(engine_db, status="waiting")
    async with engine_db.session() as session, session.begin():
        run = await session.get(Run, run_id, with_for_update=True)
        run.deadline = await db_now(session) - timedelta(seconds=1)
        run.state = {**run.state, "gate": {"key": "plan", "commands": ["/approve"]}}
    engine = runner(engine_db)
    await engine.heartbeat()
    run, _ = await rows(engine_db, run_id)
    assert run.status == "failed"
    assert run.state["gate"] is None
    assert "deadline" in run.state["failure"]
    assert await engine.claim() is None
