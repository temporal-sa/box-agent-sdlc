"""Exercise real durable HTTP admission without calling models or GitHub."""

import asyncio
import hashlib
import hmac
import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from box_agent_sdlc.db import Event, Inbox, Run, Task
from box_agent_sdlc.web import create_app


@pytest_asyncio.fixture
async def client(settings, database):
    app = create_app(settings, database)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as value:
            yield value


def authorized(settings):
    return {"X-Operator-Token": settings.operator_token}


def signed(settings, payload, *, delivery=None, event="issues"):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(settings.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {
        "X-Hub-Signature-256": signature,
        "X-GitHub-Delivery": delivery or uuid4().hex,
        "X-GitHub-Event": event,
        "Content-Type": "application/json",
    }


async def test_http_authentication_and_public_probe_boundaries(client, settings):
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/readyz")).status_code == 200
    assert (await client.get("/api/config")).status_code == 401
    assert (await client.get("/api/config", headers={"X-Operator-Token": "wrong"})).status_code == 401
    response = await client.get("/api/config", headers=authorized(settings))
    assert response.status_code == 200
    assert response.json()["mode"] == "scripted"
    assert response.json()["defaults"]["models"] == settings.agent_models()
    assert response.json()["models"] == {
        provider: [model] for provider, model in settings.provider_models.items()
    }
    assert settings.operator_token not in response.text
    assert settings.webhook_secret not in response.text


async def test_registry_proxy_trusts_only_verified_registry_header(settings, database):
    proxy = replace(settings, auth_mode="proxy", operator_token="")
    app = create_app(proxy, database)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (
            await client.get("/api/config", headers={"X-Forwarded-User": "spoofed@example.com"})
        ).status_code == 401
        assert (
            await client.get("/api/config", headers={"X-Temporal-Auth-Email": "presenter@example.com"})
        ).status_code == 200


async def test_duplicate_concurrent_webhooks_commit_one_inbox_and_task(client, settings, database):
    payload = {
        "action": "labeled",
        "repository": {"full_name": settings.github_repo},
        "issue": {"number": 42, "title": "A supported issue", "labels": [{"name": "agent"}]},
    }
    raw, headers = signed(settings, payload, delivery="duplicate-delivery")
    responses = await asyncio.gather(
        *(client.post("/webhooks/github", content=raw, headers=headers) for _ in range(4))
    )
    assert all(response.status_code == 200 for response in responses)
    assert sum(not response.json()["duplicate"] for response in responses) == 1
    # These rows must exist when the acknowledgement has already reached the caller.
    async with database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(Inbox).where(Inbox.scope == settings.scope))
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Task)
                .where(Task.scope == settings.scope, Task.kind == "webhook")
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Event)
                .where(Event.scope == settings.scope, Event.kind == "webhook_committed")
            )
            == 1
        )
        assert (
            await session.scalar(select(func.count()).select_from(Run).where(Run.scope == settings.scope))
            == 0
        )


async def test_invalid_webhook_signature_cannot_enter_inbox(client, settings, database):
    raw, headers = signed(settings, {"repository": {"full_name": settings.github_repo}})
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
    assert (await client.post("/webhooks/github", content=raw, headers=headers)).status_code == 401
    async with database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(Inbox).where(Inbox.scope == settings.scope))
            == 0
        )


@pytest.mark.parametrize("repository", [None, [], "invalid", {}, {"full_name": "other/repository"}])
async def test_malformed_or_outside_repository_is_rejected(client, settings, database, repository):
    raw, headers = signed(settings, {"repository": repository})
    response = await client.post("/webhooks/github", content=raw, headers=headers)
    assert 400 <= response.status_code < 500
    async with database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(Inbox).where(Inbox.scope == settings.scope))
            == 0
        )


async def test_webhook_identity_and_body_limits_are_enforced(client, settings):
    raw, headers = signed(settings, {"repository": {"full_name": settings.github_repo}})
    headers.pop("X-GitHub-Delivery")
    assert (await client.post("/webhooks/github", content=raw, headers=headers)).status_code == 400
    oversized = b"x" * (settings.max_body_bytes + 1)
    assert (await client.post("/webhooks/github", content=oversized)).status_code == 413


async def test_scenario_retry_uses_one_run_and_one_durable_initial_task(client, settings, database):
    headers = {**authorized(settings), "Idempotency-Key": "same-user-action"}
    request = {"kind": "review", "title": "Review only", "budget_usd": 1}
    first = await client.post("/api/scenarios", json=request, headers=headers)
    second = await client.post("/api/scenarios", json=request, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["run_id"] == second.json()["run_id"]
    async with database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(Run).where(Run.scope == settings.scope))
            == 1
        )
        assert (
            await session.scalar(select(func.count()).select_from(Task).where(Task.scope == settings.scope))
            == 1
        )


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "unknown"},
        {"kind": "review", "repo": "other/repo"},
        {"kind": "issue", "budget_usd": 0},
        {"kind": "issue", "budget_usd": 26},
        {"kind": "issue", "autonomy": "unchecked"},
        {"kind": "issue", "models": {"coder": {"provider": "unsupported"}}},
    ],
)
async def test_invalid_scenarios_cannot_schedule_paid_work(client, settings, database, body):
    response = await client.post("/api/scenarios", json=body, headers=authorized(settings))
    assert response.status_code in {409, 422}
    async with database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(Run).where(Run.scope == settings.scope))
            == 0
        )
        assert (
            await session.scalar(select(func.count()).select_from(Task).where(Task.scope == settings.scope))
            == 0
        )


async def test_run_scope_blocks_access_and_commands_from_another_app(client, settings, database):
    original = await client.post("/api/scenarios", json={"kind": "review"}, headers=authorized(settings))
    run_id = original.json()["run_id"]
    other = replace(settings, scope=f"other-{uuid4().hex}")
    app = create_app(other, database)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as other_client,
    ):
        assert (
            await other_client.get(f"/api/runs/{run_id}", headers=authorized(settings))
        ).status_code == 404
        assert (
            await other_client.post(
                f"/api/runs/{run_id}/commands", json={"command": "/stop"}, headers=authorized(settings)
            )
        ).status_code == 404


async def test_duplicate_operator_command_is_admitted_once(client, settings, database):
    created = await client.post("/api/scenarios", json={"kind": "review"}, headers=authorized(settings))
    run_id = created.json()["run_id"]
    headers = {**authorized(settings), "Idempotency-Key": "one-stop-click"}
    for _ in range(2):
        response = await client.post(
            f"/api/runs/{run_id}/commands", json={"command": "/stop"}, headers=headers
        )
        assert response.status_code == 200
    async with database.session() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Task)
                .where(Task.scope == settings.scope, Task.kind == "command")
            )
            == 1
        )
