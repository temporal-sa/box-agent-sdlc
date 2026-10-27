"""Exercise public pipeline behavior through actual PostgreSQL task boundaries."""

import os
import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from box_agent_sdlc.config import Settings
from box_agent_sdlc.db import Database, Environment, Fault, Run, Scope, Task, db_now
from box_agent_sdlc.engine import Engine, ensure_scope
from box_agent_sdlc.pipeline import Pipeline
from box_agent_sdlc.state import create_run


@pytest.fixture
async def pipeline_db():
    settings = Settings(
        database_url=os.getenv("TEST_DATABASE_URL", "postgresql://box:box@localhost:5432/box_sdlc"),
        scope="pipeline-" + uuid.uuid4().hex[:12],
        auth_mode="development",
        mode="scripted",
        bake_seconds=0,
        check_seconds=0,
        review_window=0,
    )
    db = Database(settings)
    async with db.session() as session, session.begin():
        await ensure_scope(session, settings.scope)
    yield db
    await db.close()


async def start(db, kind="issue", **request):
    async with db.session() as s, s.begin():
        run = await create_run(s, db.settings, {"kind": kind, **request})
        return run.id


async def view(db, run_id):
    async with db.session() as s:
        run = await s.get(Run, run_id)
        return run


async def drain(db, max_steps=200):
    pipeline = Pipeline(db)
    engine = Engine(db, pipeline.handle, pipeline.advance)
    count = 0
    while count < max_steps:
        claim = await engine.claim()
        if not claim:
            return count
        await engine.execute(claim)
        count += 1
    raise AssertionError("Pipeline did not settle within its bounded step count")


async def say(db, run_id, command, *, authorized=True):
    async with db.session() as s, s.begin():
        run = await s.get(Run, run_id)
        await Pipeline(db).receive_comment(
            s,
            run,
            {
                "external_id": uuid.uuid4().hex,
                "author": "presenter",
                "body": command,
                "authorized": authorized,
            },
        )
    await drain(db)


async def reach_merge(db, run_id):
    await drain(db)
    assert (await view(db, run_id)).state["gate"]["key"] == "plan"
    await say(db, run_id, "/approve")
    run = await view(db, run_id)
    assert run.state["gate"]["key"] == "merge", run.state
    assert run.state["approved_sha"] == run.state["tests_sha"]
    assert all(x["status"] == "completed" for x in run.state["lanes"].values())


async def reach_release(db, run_id):
    await reach_merge(db, run_id)
    await say(db, run_id, "/merge")
    assert (await view(db, run_id)).state["gate"]["key"] == "production"


async def test_issue_happy_path_across_runner_restarts(pipeline_db):
    db = pipeline_db
    run_id = await start(db)
    await reach_release(db, run_id)
    # Every drain creates a new engine identity, like a runner restart at the gates.
    await say(db, run_id, "/ship")
    run = await view(db, run_id)
    assert run.status == "completed"
    assert run.spent == 0
    async with db.session() as s:
        env = await s.get(Environment, db.settings.scope)
        assert env.owner is None
        assert env.state["prod"] == run.state["merge_sha"]


async def test_bad_canary_runs_recorded_compensation(pipeline_db):
    db = pipeline_db
    run_id = await start(db)
    await reach_release(db, run_id)
    async with db.session() as s, s.begin():
        s.add(Fault(run_id=run_id, kind="canary_failure", point="canary", actor="test"))
    await say(db, run_id, "/ship")
    run = await view(db, run_id)
    assert run.status == "rolled_back"
    async with db.session() as s:
        tasks = list(await s.scalars(select(Task).where(Task.run_id == run_id, Task.key.like("%rollback%"))))
        assert len(tasks) == 3
        assert all(t.status == "completed" for t in tasks)
        env = await s.get(Environment, db.settings.scope)
        assert env.state.get("canary") is None
        assert env.state.get("prod") is None


@pytest.mark.parametrize("kind", ["dependency", "review"])
async def test_other_scenarios_reach_durable_conversation(pipeline_db, kind):
    db = pipeline_db
    run_id = await start(db, kind)
    await drain(db)
    run = await view(db, run_id)
    assert run.status == "waiting", run.state
    assert run.state["gate"]["key"] == "conversation", run.state
    assert run.state["review"]["verdict"] == "approve"
    if kind == "dependency":
        assert "diagnosis" in run.state
    await say(db, run_id, "Please improve the test coverage")
    run = await view(db, run_id)
    assert run.state["gate"]["key"] == "conversation", run.state
    assert run.state["review_round"] > 0


async def test_unauthorized_and_wrong_gate_commands_cannot_advance(pipeline_db):
    db = pipeline_db
    run_id = await start(db)
    await drain(db)
    await say(db, run_id, "/approve", authorized=False)
    assert (await view(db, run_id)).state["gate"]["key"] == "plan"
    await say(db, run_id, "/ship")
    assert (await view(db, run_id)).state["gate"]["key"] == "plan"
    await say(db, run_id, "/stop")
    assert (await view(db, run_id)).status == "stopped"


async def test_scope_change_requires_new_plan_approval(pipeline_db):
    db = pipeline_db
    run_id = await start(db)
    await reach_merge(db, run_id)
    await say(db, run_id, "scope: also add a loyalty program")
    run = await view(db, run_id)
    assert run.state["plan_revision"] == 2
    assert run.state["gate"]["key"] == "plan"
    assert run.state.get("approved_sha") is None


async def test_version_is_pinned_per_run(pipeline_db):
    db = pipeline_db
    old = await start(db)
    async with db.session() as s, s.begin():
        scope = await s.get(Scope, db.settings.scope)
        scope.version = "v2"
    new = await start(db)
    await drain(db)
    assert (await view(db, old)).version == "v1"
    assert (await view(db, new)).version == "v2"
    assert (await view(db, old)).state.get("plan")
    assert not (await view(db, new)).state.get("plan")
    # v2 tasks are not accidentally claimed by v1 runners.
    v2db = Database(replace(db.settings, version="v2"))
    try:
        await drain(v2db)
        assert (await view(db, new)).state["gate"]["key"] == "plan"
        await say(v2db, new, "/approve")
        assert (await view(db, new)).state["security_review"]["verdict"] == "approve"
    finally:
        await v2db.close()


async def test_simultaneous_run_creation_obeys_active_cap(pipeline_db):
    import asyncio

    db = pipeline_db
    results = await asyncio.gather(*(start(db) for _ in range(6)), return_exceptions=True)
    assert sum(isinstance(x, str) for x in results) == 3
    assert sum(isinstance(x, ValueError) for x in results) == 3


async def test_stale_canary_owner_cannot_change_new_release(pipeline_db):
    db = pipeline_db
    run_id = await start(db)
    await reach_release(db, run_id)
    async with db.session() as s, s.begin():
        env = await s.get(Environment, db.settings.scope)
        env.owner, env.generation = "another-run", env.generation + 1
    await say(db, run_id, "/ship")
    run = await view(db, run_id)
    assert run.status == "failed"
    async with db.session() as s:
        assert (await s.get(Environment, db.settings.scope)).owner == "another-run"


async def test_canary_timer_is_rechecked_after_downtime(pipeline_db):
    db = pipeline_db
    run_id = await start(db)
    await reach_release(db, run_id)
    db.settings = replace(db.settings, bake_seconds=120, check_seconds=120)
    await say(db, run_id, "/ship")
    assert (await view(db, run_id)).status == "running"
    async with db.session() as s, s.begin():
        run = await s.get(Run, run_id)
        stamp = await db_now(s)
        run.state = {**run.state, "canary_deadline": (stamp - timedelta(seconds=1)).isoformat()}
        await s.execute(
            update(Task).where(Task.run_id == run_id, Task.status == "ready").values(ready_at=stamp)
        )
    await drain(db)
    run = await view(db, run_id)
    assert run.status == "completed"
    assert run.state["last_health_check"]


async def test_second_release_waits_without_exhausting_task_retries(pipeline_db):
    db = pipeline_db
    first = await start(db)
    await reach_release(db, first)
    second = await start(db)
    await reach_merge(db, second)
    await say(db, second, "/merge")
    assert (await view(db, second)).state["gate"]["key"] == "environment"
    await say(db, first, "/ship")
    async with db.session() as s, s.begin():
        await s.execute(
            update(Task).where(Task.run_id == second, Task.status == "ready").values(ready_at=await db_now(s))
        )
    await drain(db)
    assert (await view(db, second)).state["gate"]["key"] == "production"
