"""Regression coverage for late results, stale approvals, and release compensation."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.orm.attributes import flag_modified

from box_agent_sdlc.db import Comment, Environment, Inbox, Run, Task, db_now
from box_agent_sdlc.engine import Engine
from box_agent_sdlc.pipeline import Pipeline
from box_agent_sdlc.state import create_run
from box_agent_sdlc.web import create_app


async def edge_run(database, **state):
    async with database.session() as session, session.begin():
        run = await create_run(session, database.settings, {"kind": "issue", "title": "Pipeline boundary"})
        await session.execute(update(Task).where(Task.run_id == run.id).values(status="cancelled"))
        run.state = {
            **run.state,
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "base_branch": "runs/base",
            "head_branch": "runs/integration",
            "review_round": 0,
            "review_sha": "b" * 40,
            "approved_sha": "b" * 40,
            "tests_sha": "b" * 40,
            "pr_number": 17,
            "refs": ["pr:17"],
            **state,
        }
        return run.id


async def send_one_command(database, run_id, command):
    pipeline = Pipeline(database)
    async with database.session() as session, session.begin():
        run = await session.get(Run, run_id, with_for_update=True)
        await pipeline.receive_comment(
            session,
            run,
            {
                "external_id": "operator:" + uuid4().hex,
                "author": "operator",
                "body": command,
                "authorized": True,
            },
        )
    engine = Engine(database, pipeline.handle, pipeline.advance)
    claim = await engine.claim()
    assert claim and claim.kind == "command"
    await engine.execute(claim)


async def test_fresh_merge_approval_can_run_after_stale_merge_result(database):
    run_id = await edge_run(database)
    pipeline = Pipeline(database)
    async with database.session() as session, session.begin():
        run = await session.get(Run, run_id, with_for_update=True)
        await pipeline.stage(session, run, "merge")
        task = await session.scalar(select(Task).where(Task.run_id == run_id, Task.status == "ready"))
        task.status = "completed"
        await pipeline.advance(session, task, run, {"stale": True})
        stamp = await db_now(session)
        # A later exact-SHA review passed. Ignore the already-tested re-review mechanics.
        run.state = {
            **run.state,
            "approved_sha": "c" * 40,
            "tests_sha": "c" * 40,
            "review_sha": "c" * 40,
            "gate": {
                "id": "fresh-merge",
                "key": "merge",
                "commands": ["/merge"],
                "head_sha": "c" * 40,
                "waiting_since": stamp.timestamp(),
            },
        }
        run.status = "waiting"
        await session.execute(
            update(Task).where(Task.run_id == run_id, Task.status == "ready").values(status="cancelled")
        )
    await send_one_command(database, run_id, "/merge")
    async with database.session() as session:
        tasks = list(await session.scalars(select(Task).where(Task.run_id == run_id)))
    new_merge = [task for task in tasks if task.payload.get("stage") == "merge" and task.status == "ready"]
    assert new_merge, (
        "Fresh approval must schedule work rather than collide with the completed stale merge key"
    )


class DirectEffects:
    def __init__(self, database):
        self.claim = SimpleNamespace(scope=database.settings.scope, key="catchup")

    async def effect(self, name, request, execute, *args, **kwargs):
        return await execute()


async def test_comment_catchup_deduplicates_the_same_webhook_approval(database):
    run_id = await edge_run(database, issue_number=12, pr_number=None, plan_revision=2)
    async with database.session() as session:
        stamp = await db_now(session)
    old_time = (stamp - timedelta(hours=1)).isoformat()

    class GitHubComments:
        async def fetch_comments(self, *args, **kwargs):
            return [
                {
                    "id": "issue:42",
                    "author": "approved-presenter",
                    "body": "/approve",
                    "url": "https://github.com/example/repo/issues/12#issuecomment-42",
                    "created_at": old_time,
                }
            ]

        async def actor_can_approve(self, *args):
            return True

    pipeline = Pipeline(database, github=GitHubComments())
    async with database.session() as session, session.begin():
        run = await session.get(Run, run_id, with_for_update=True)
        run.state = {
            **run.state,
            "gate": {
                "id": "revised-plan",
                "key": "plan",
                "commands": ["/approve"],
                "waiting_since": stamp.timestamp(),
            },
        }
        inbox = Inbox(
            id=uuid4().hex,
            scope=run.scope,
            event="issue_comment",
            payload={
                "action": "created",
                "repository": {"full_name": database.settings.github_repo},
                "issue": {"number": 12},
                "comment": {
                    "id": 42,
                    "body": "/approve",
                    "created_at": old_time,
                    "user": {"login": "approved-presenter"},
                },
            },
        )
        session.add(inbox)
        inbox_id = inbox.id
    webhook_result = await pipeline.webhook(DirectEffects(database), {"inbox_id": inbox_id})
    async with database.session() as session, session.begin():
        run = await session.get(Run, run_id, with_for_update=True)
        await pipeline.receive_comment(session, run, webhook_result["comment"])
    async with database.session() as session:
        run = await session.get(Run, run_id)
    result = await pipeline.live_stage(DirectEffects(database), run, "catchup", {})
    async with database.session() as session, session.begin():
        run = await session.get(Run, run_id, with_for_update=True)
        for comment in result.get("comments", []):
            await pipeline.receive_comment(session, run, comment)
    async with database.session() as session:
        comments = list(await session.scalars(select(Comment).where(Comment.run_id == run_id)))
    assert len(comments) <= 1, "One GitHub approval must not receive separate webhook and catchup identities"


async def test_historical_approval_cannot_approve_a_revised_plan(database):
    async with database.session() as session:
        stamp = await db_now(session)
    run_id = await edge_run(
        database,
        plan_revision=2,
        gate={"id": "new-plan", "key": "plan", "commands": ["/approve"], "waiting_since": stamp.timestamp()},
    )
    pipeline = Pipeline(database)
    async with database.session() as session, session.begin():
        run = await session.get(Run, run_id, with_for_update=True)
        run.status = "waiting"
        await pipeline.receive_comment(
            session,
            run,
            {
                "external_id": "issue:old-approval",
                "author": "approved-presenter",
                "body": "/approve",
                "authorized": True,
                "created_at": (stamp - timedelta(hours=1)).isoformat(),
            },
        )
    engine = Engine(database, pipeline.handle, pipeline.advance)
    claim = await engine.claim()
    assert claim and claim.kind == "command"
    await engine.execute(claim)
    async with database.session() as session:
        run = await session.get(Run, run_id)
        pending = list(
            await session.scalars(select(Task).where(Task.run_id == run_id, Task.status == "ready"))
        )
    assert run.state["gate"]["id"] == "new-plan"
    assert not any(task.payload.get("stage") == "create_lanes" for task in pending)


async def test_replanning_prepares_the_new_branch_before_planner_tools(database):
    run_id = await edge_run(database, pr_number=None, pending_feedback="Add a requirement")
    branches = {"runs/integration": "a" * 40}

    class Branches:
        async def ensure_branch(self, repo, branch, sha, **kwargs):
            branches[branch] = sha
            return {"sha": sha}

        async def get_ref_if_exists(self, repo, branch):
            return {"sha": branches[branch]} if branch in branches else None

    pipeline = Pipeline(database, github=Branches())
    async with database.session() as session, session.begin():
        run = await session.get(Run, run_id, with_for_update=True)
        run.mode = "live"
        await pipeline.after_agent(session, run, {"stage": "triage"}, {"kind": "scope_change"})
        flag_modified(run, "state")

    async def handler(context, run, payload):
        if context.claim.kind == "agent":
            assert run.state["head_branch"] in branches, (
                "Planner tools need an existing durable revision branch"
            )
            return {
                "done": True,
                "checkpoint": {"messages": []},
                "usage": {},
                "output": {
                    "summary": "Revised requirement",
                    "acceptance_criteria": ["New requirement works"],
                    "subtasks": [
                        {
                            "id": "core",
                            "title": "Implement",
                            "description": "Implement the change",
                            "files": [],
                        }
                    ],
                },
            }
        return await pipeline.live_stage(context, run, payload["stage"], payload)

    engine = Engine(database, handler, pipeline.advance)
    # The branch preparation and subsequent planning are separate durable tasks.
    for _ in range(2):
        claim = await engine.claim()
        assert claim
        await engine.execute(claim)
    async with database.session() as session:
        run = await session.get(Run, run_id)
    assert run.state["head_branch"] in branches
    assert run.state["gate"]["key"] == "plan"


async def test_late_old_reviewer_cannot_certify_the_new_head(database):
    run_id = await edge_run(database, review=None)
    pipeline = Pipeline(database)
    async with database.session() as session, session.begin():
        run = await session.get(Run, run_id, with_for_update=True)
        await pipeline.start_agent(session, run, "reviewer", "review", {"diff": "old diff"}, suffix="0")
        flag_modified(run, "state")
    entered, finish = asyncio.Event(), asyncio.Event()

    async def delayed_review(context, run, payload):
        entered.set()
        await finish.wait()
        return {
            "done": True,
            "checkpoint": {"messages": []},
            "usage": {},
            "output": {"verdict": "approve", "summary": "old diff approval", "comments": []},
        }

    engine = Engine(database, delayed_review, pipeline.advance)
    claim = await engine.claim()
    assert claim and claim.kind == "agent"
    task = asyncio.create_task(engine.execute(claim))
    await asyncio.wait_for(entered.wait(), timeout=5)
    # A synchronize event and the next review-input stage have committed a newer head.
    async with database.session() as session, session.begin():
        run = await session.get(Run, run_id, with_for_update=True)
        run.state = {
            **run.state,
            "review_sha": "c" * 40,
            "head_sha": "c" * 40,
            "review_round": 1,
            "approved_sha": None,
            "review": None,
            "security_review": None,
        }
    finish.set()
    await asyncio.wait_for(task, timeout=5)
    async with database.session() as session:
        run = await session.get(Run, run_id)
        tasks = list(await session.scalars(select(Task).where(Task.run_id == run_id, Task.status == "ready")))
    assert not run.state.get("review") or run.state["review"].get("summary") != "old diff approval"
    assert not any(item.payload.get("stage") == "publish_review" for item in tasks)


async def test_inflight_canary_cannot_promote_after_stop(database):
    async with database.session() as session:
        stamp = await db_now(session)
    run_id = await edge_run(
        database,
        merge_sha="b" * 40,
        release_generation=1,
        canary_deadline=(stamp - timedelta(seconds=1)).isoformat(),
    )
    pipeline = Pipeline(database)
    async with database.session() as session, session.begin():
        run = await session.get(Run, run_id, with_for_update=True)
        session.add(
            Environment(
                scope=run.scope,
                owner=run.id,
                generation=1,
                state={"prod": "a" * 40, "canary": "b" * 40, "weight": 20},
            )
        )
        await pipeline.stage(session, run, "canary_check", suffix="0", tick=0)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def delayed_check(context, run, payload):
        entered.set()
        await finish.wait()
        return {"healthy": True, "sha": "b" * 40}

    engine = Engine(database, delayed_check, pipeline.advance)
    claim = await engine.claim()
    assert claim
    check = asyncio.create_task(engine.execute(claim))
    await asyncio.wait_for(entered.wait(), timeout=5)
    await send_one_command(database, run_id, "/stop operator rejected this release")
    finish.set()
    await asyncio.wait_for(check, timeout=5)
    async with database.session() as session:
        run = await session.get(Run, run_id)
        environment = await session.get(Environment, database.settings.scope)
    assert environment.state.get("prod") == "a" * 40, (
        "A stopped release cannot be promoted by a late healthy check"
    )
    assert run.status != "completed"


async def test_expiry_does_not_abandon_an_owned_canary(database):
    run_id = await edge_run(database, merge_sha="b" * 40, release_generation=1)
    async with database.session() as session, session.begin():
        run = await session.get(Run, run_id, with_for_update=True)
        run.status = "waiting"
        run.deadline = await db_now(session) - timedelta(seconds=1)
        session.add(
            Environment(
                scope=run.scope,
                owner=run.id,
                generation=1,
                state={"prod": "a" * 40, "canary": "b" * 40, "weight": 20},
            )
        )
    pipeline = Pipeline(database)
    await Engine(database, pipeline.handle, pipeline.advance).heartbeat()
    async with database.session() as session:
        run = await session.get(Run, run_id)
        environment = await session.get(Environment, database.settings.scope)
        pending = list(
            await session.scalars(select(Task).where(Task.run_id == run_id, Task.status == "ready"))
        )
    assert environment.state.get("prod") == "a" * 40
    if run.status == "failed":
        assert environment.owner is None
        assert not environment.state.get("canary")
    else:
        assert any(
            "rollback" in item.payload.get("stage", "") or "compensat" in item.payload.get("stage", "")
            for item in pending
        )


async def test_exhausted_crash_recovery_does_not_abandon_release_ownership(database):
    run_id = await edge_run(database, merge_sha="b" * 40, release_generation=1)
    pipeline = Pipeline(database)
    async with database.session() as session, session.begin():
        run = await session.get(Run, run_id, with_for_update=True)
        session.add(
            Environment(
                scope=run.scope,
                owner=run.id,
                generation=1,
                state={"prod": "a" * 40, "canary": "b" * 40, "weight": 20},
            )
        )
        await pipeline.stage(session, run, "canary_check", suffix="0", tick=0)
        task = await session.scalar(select(Task).where(Task.run_id == run_id, Task.status == "ready"))
        task.attempt = task.max_attempts
        task.status = "leased"
        task.owner = "dead-worker"
        task.lease_until = await db_now(session) - timedelta(seconds=1)
    await Engine(database, pipeline.handle, pipeline.advance).claim()
    async with database.session() as session:
        run = await session.get(Run, run_id)
        environment = await session.get(Environment, database.settings.scope)
        pending = list(
            await session.scalars(select(Task).where(Task.run_id == run_id, Task.status == "ready"))
        )
    assert environment.state.get("prod") == "a" * 40
    if run.status == "failed":
        assert environment.owner is None
        assert not environment.state.get("canary")
    else:
        assert any("rollback" in item.payload.get("stage", "") for item in pending)


@pytest.mark.parametrize("change", ["stop", "revision"])
async def test_claimed_task_cannot_start_a_new_effect_after_invalidation(database, change):
    run_id = await edge_run(database)
    pipeline = Pipeline(database)
    async with database.session() as session, session.begin():
        run = await session.get(Run, run_id, with_for_update=True)
        await pipeline.stage(session, run, "merge")
    entered, finish = asyncio.Event(), asyncio.Event()
    remote_calls = []

    async def handler(context, run, payload):
        entered.set()
        await finish.wait()

        async def remote_mutation():
            remote_calls.append("merge")
            return {"sha": "b" * 40}

        return await context.effect("merge", {"sha": "b" * 40}, remote_mutation)

    engine = Engine(database, handler, pipeline.advance)
    claim = await engine.claim()
    assert claim
    active = asyncio.create_task(engine.execute(claim))
    await asyncio.wait_for(entered.wait(), timeout=5)
    if change == "stop":
        await send_one_command(database, run_id, "/stop")
    else:
        async with database.session() as session, session.begin():
            run = await session.get(Run, run_id, with_for_update=True)
            run.state = {**run.state, "plan_revision": 2, "approved_sha": None}
    finish.set()
    await asyncio.wait_for(active, timeout=5)
    assert not remote_calls, "A valid task lease must not authorize new external work after run invalidation"


async def test_repository_html_receives_an_opaque_origin_sandbox(settings, database):
    async with database.session() as session, session.begin():
        session.add(Environment(scope=settings.scope, owner=None, generation=1, state={"staging": "a" * 40}))
    app = create_app(settings, database)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        response = await client.get(
            "/apps/gh/staging/index.html", headers={"X-Operator-Token": settings.operator_token}
        )
    assert response.status_code == 200
    policy = response.headers.get("content-security-policy", "")
    sandbox = next((item.strip() for item in policy.split(";") if item.strip().startswith("sandbox")), "")
    assert sandbox, "Generated repository HTML must not execute with console origin authority"
    assert "allow-same-origin" not in sandbox
    assert "allow-forms" not in sandbox
    assert "allow-top-navigation" not in sandbox


@pytest.mark.parametrize("origin", ["null", "https://untrusted.example"])
async def test_cross_origin_documents_cannot_use_operator_mutations(settings, database, origin):
    app = create_app(settings, database)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        response = await client.post(
            "/api/scenarios",
            json={"kind": "issue"},
            headers={
                "X-Operator-Token": settings.operator_token,
                "Origin": origin,
                "Sec-Fetch-Site": "cross-site",
            },
        )
    assert response.status_code == 403
