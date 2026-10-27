"""Read-only console snapshots and durable authenticated command/webhook admission."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from .config import Settings
from .db import (
    Asset,
    Database,
    Environment,
    Event,
    Fault,
    Inbox,
    Run,
    Scope,
    Task,
    Worker,
    db_now,
    emit,
    uid,
)
from .engine import TERMINAL, enqueue, ensure_scope
from .state import create_run, snapshot


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = "issue"
    title: str = Field(default="", max_length=300)
    body: str = Field(default="", max_length=20000)
    repo: str | None = None
    models: dict = Field(default_factory=dict)
    autonomy: str = "supervised"
    budget_usd: float = Field(default=3, gt=0, le=25)


class CommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=8000)


class FaultRequest(BaseModel):
    kind: str
    point: str | None = None


class VersionRequest(BaseModel):
    version: str


def create_app(settings: Settings | None = None, db: Database | None = None):
    settings = settings or Settings()
    database = db or Database(settings)

    @asynccontextmanager
    async def lifespan(app):
        settings.validate()
        # Startup is not allowed to create paid work or external resources.
        async with database.session() as s, s.begin():
            await ensure_scope(s, settings.scope)
        yield
        if db is None:
            await database.close()

    app = FastAPI(title="Box Agentic SDLC — PostgreSQL baseline", lifespan=lifespan)
    app.state.db = database

    @app.middleware("http")
    async def browser_origin_boundary(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            allowed = {settings.public_url.rstrip("/")}
            if settings.auth_mode == "development":
                allowed.update({"http://localhost:5173", "http://127.0.0.1:5173"})
            if (origin and origin not in allowed) or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse(
                    {"detail": "Browser origin cannot operate this application"}, status_code=403
                )
        return await call_next(request)

    async def operator(request: Request):
        if settings.auth_mode == "development" and settings.mode == "scripted":
            return "local-presenter"
        if settings.auth_mode == "proxy":
            # Registry ingress strips client auth headers before verified forward-auth.
            user = request.headers.get(settings.proxy_user_header)
            if user and len(user) <= 200:
                return user
        token = request.headers.get("X-Operator-Token", "")
        if settings.operator_token and hmac.compare_digest(token, settings.operator_token):
            return "operator-token"
        raise HTTPException(401, "Authenticate through registry SSO or provide X-Operator-Token")

    async def get_run(s, run_id, *, lock=False):
        run = await s.get(Run, run_id, with_for_update=lock)
        if not run or run.scope != settings.scope:
            raise HTTPException(404, "Run not found")
        return run

    @app.get("/healthz")
    async def health():
        return {"status": "alive", "mode": settings.mode}

    @app.get("/readyz")
    async def ready():
        try:
            async with database.session() as s:
                revision = await s.scalar(text("SELECT version_num FROM alembic_version"))
                await s.execute(select(Scope.id).limit(1))
            if revision != "0002":
                raise ValueError("Unsupported schema revision")
        except Exception:
            return JSONResponse(
                {"status": "not_ready", "detail": "Database or compatible schema unavailable"},
                status_code=503,
            )
        return {"status": "ready", "schema": revision}

    @app.get("/api/config", dependencies=[Depends(operator)])
    async def config():
        return {
            "name": "box-agent-sdlc",
            "mode": settings.mode,
            "scope": settings.scope,
            "auth_mode": settings.auth_mode,
            "versions": ["v1", "v2"],
            "providers": ["anthropic", "openai", "google"],
            "models": {provider: [model] for provider, model in settings.provider_models.items()},
            "github_repo": settings.github_repo,
            "max_active_runs": settings.max_runs,
            "defaults": {
                "budget_usd": settings.default_budget,
                "review_window_seconds": settings.review_window,
                "canary_bake_seconds": settings.bake_seconds,
                "models": settings.agent_models(),
            },
            "release_semantics": "Storefront commit pointers and simulated routing; application deployment is managed by registry",
        }

    @app.get("/api/overview", dependencies=[Depends(operator)])
    async def overview():
        async with database.session() as s:
            runs = list(
                await s.scalars(
                    select(Run).where(Run.scope == settings.scope).order_by(Run.created_at.desc()).limit(100)
                )
            )
            workers = list(await s.scalars(select(Worker).where(Worker.scope == settings.scope)))
            stamp = await db_now(s)
            scope = await s.get(Scope, settings.scope)
            return {
                "runs": [snapshot(r) for r in runs],
                "mode": settings.mode,
                "version": scope.version,
                "workers": [
                    {
                        "id": w.id,
                        "version": w.version,
                        "heartbeat": w.heartbeat.timestamp(),
                        "online": (stamp - w.heartbeat).total_seconds() < settings.heartbeat_seconds * 3,
                    }
                    for w in workers
                ],
            }

    @app.get("/api/runs/{run_id}", dependencies=[Depends(operator)])
    async def detail(run_id: str):
        async with database.session() as s:
            run = await get_run(s, run_id)
            events = list(
                await s.scalars(
                    select(Event)
                    .where(Event.scope == settings.scope, Event.run_id == run_id)
                    .order_by(Event.id.desc())
                    .limit(300)
                )
            )
            tasks = list(await s.scalars(select(Task).where(Task.run_id == run_id).order_by(Task.ready_at)))
            return {
                **snapshot(run),
                "events": [
                    {"id": e.id, "kind": e.kind, "at": e.at.timestamp(), "data": e.data}
                    for e in reversed(events)
                ],
                "attempts": [
                    {
                        "key": t.key,
                        "kind": t.kind,
                        "attempt": t.attempt,
                        "status": t.status,
                        "fence": t.fence,
                        "error": t.error,
                        "ready_at": t.ready_at.timestamp(),
                    }
                    for t in tasks
                ],
            }

    @app.post("/api/scenarios")
    async def scenario(body: Scenario, request: Request, actor=Depends(operator)):
        if body.autonomy not in {"auto", "supervised"}:
            raise HTTPException(422, "Unknown autonomy")
        if set(body.models) - {"planner", "coder", "reviewer"}:
            raise HTTPException(422, "Unknown agent role")
        for choice in body.models.values():
            if not isinstance(choice, dict) or set(choice) - {"provider", "model"}:
                raise HTTPException(422, "Each role model must specify provider and model")
            if choice.get("provider", "anthropic") not in {"anthropic", "openai", "google"}:
                raise HTTPException(422, "Unsupported model provider")
            if "model" in choice and (
                not isinstance(choice["model"], str) or not 1 <= len(choice["model"].strip()) <= 120
            ):
                raise HTTPException(422, "Invalid model name")
        key = request.headers.get("Idempotency-Key")
        if key and len(key) > 200:
            raise HTTPException(422, "Idempotency key too long")
        run_id = f"{settings.scope}.request-{hashlib.sha256(key.encode()).hexdigest()[:24]}" if key else None
        try:
            async with database.session() as s, s.begin():
                run = await create_run(s, settings, body.model_dump(), run_id=run_id)
                await emit(s, settings.scope, run.id, "operator_scenario", actor=actor)
                return {"run_id": run.id, "run": snapshot(run)}
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/runs/{run_id}/commands")
    async def command(run_id: str, body: CommandRequest, request: Request, actor=Depends(operator)):
        key = request.headers.get("Idempotency-Key") or uid()
        if len(key) > 200:
            raise HTTPException(422, "Idempotency key too long")
        async with database.session() as s, s.begin():
            run = await get_run(s, run_id, lock=True)
            if run.status in TERMINAL:
                raise HTTPException(409, "Run has finished")
            from .pipeline import Pipeline

            await Pipeline(database).receive_comment(
                s,
                run,
                {
                    "external_id": "operator:" + key,
                    "author": actor,
                    "body": body.command,
                    "authorized": True,
                    "url": "",
                },
            )
            return {"accepted": True, "run_id": run.id}

    @app.post("/api/runs/{run_id}/faults")
    async def fault(run_id: str, body: FaultRequest, actor=Depends(operator)):
        defaults = {
            "worker_exit": "before_task",
            "provider_429": "model",
            "executor_failure": "executor",
            "canary_failure": "canary",
        }
        if body.kind not in defaults or (
            body.point and body.point not in {"before_task", "after_effect", "model", "executor", "canary"}
        ):
            raise HTTPException(422, "Unsupported fault kind or point")
        async with database.session() as s, s.begin():
            run = await get_run(s, run_id, lock=True)
            if run.status in TERMINAL:
                raise HTTPException(409, "Run has finished")
            item = Fault(run_id=run.id, kind=body.kind, point=body.point or defaults[body.kind], actor=actor)
            s.add(item)
            await s.flush()
            await emit(
                s,
                run.scope,
                run.id,
                "fault_requested",
                fault_id=item.id,
                kind=body.kind,
                point=item.point,
                actor=actor,
            )
            return {"accepted": True, "fault_id": item.id}

    @app.post("/api/version")
    async def version(body: VersionRequest, actor=Depends(operator)):
        if body.version not in {"v1", "v2"}:
            raise HTTPException(422, "Version must be v1 or v2")
        async with database.session() as s, s.begin():
            scope = await s.get(Scope, settings.scope, with_for_update=True)
            scope.version = body.version
            await emit(s, settings.scope, None, "version_selected", version=body.version, actor=actor)
        return {"version": body.version}

    @app.post("/api/reset")
    async def reset(actor=Depends(operator)):
        # Reset is scoped and preserves the audit trail. It never deletes shared GitHub history.
        async with database.session() as s, s.begin():
            runs = list(
                await s.scalars(
                    select(Run)
                    .where(Run.scope == settings.scope, Run.status.not_in(TERMINAL))
                    .with_for_update()
                )
            )
            from .pipeline import Pipeline

            pipeline = Pipeline(database)
            for run in runs:
                await pipeline.receive_comment(
                    s,
                    run,
                    {
                        "external_id": "reset:" + uid(),
                        "author": actor,
                        "body": "/stop reset",
                        "authorized": True,
                    },
                )
            return {"accepted": len(runs)}

    @app.post("/webhooks/github")
    async def webhook(request: Request):
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > settings.max_body_bytes):
            raise HTTPException(413, "Webhook too large")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > settings.max_body_bytes:
                raise HTTPException(413, "Webhook too large")
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(settings.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
        if not settings.webhook_secret or not hmac.compare_digest(signature, expected):
            raise HTTPException(401, "Invalid webhook signature")
        delivery = request.headers.get("X-GitHub-Delivery", "")
        event = request.headers.get("X-GitHub-Event", "")
        if not delivery or len(delivery) > 150 or len(event) > 60:
            raise HTTPException(400, "Missing or invalid delivery identity")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid JSON")
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("repository"), dict)
            or payload["repository"].get("full_name") != settings.github_repo
        ):
            raise HTTPException(403, "Repository outside allowlist")
        identity = settings.scope + ":" + delivery
        async with database.session() as s, s.begin():
            accepted = await s.scalar(
                insert(Inbox)
                .values(id=identity, scope=settings.scope, event=event, payload=payload)
                .on_conflict_do_nothing()
                .returning(Inbox.id)
            )
            if accepted:
                await enqueue(
                    s, settings.scope, None, "webhook:" + delivery, "webhook", {"inbox_id": identity}
                )
                await emit(s, settings.scope, None, "webhook_committed", delivery=delivery, event=event)
        return {"accepted": True, "duplicate": not bool(accepted)}

    @app.get("/api/runs/{run_id}/stream")
    async def stream(run_id: str, request: Request, actor=Depends(operator)):
        try:
            cursor = int(request.headers.get("Last-Event-ID", "0"))
        except ValueError:
            raise HTTPException(400, "Invalid event offset")
        async with database.session() as s:
            await get_run(s, run_id)

        async def events():
            nonlocal cursor
            first = True
            while not await request.is_disconnected():
                async with database.session() as s:
                    rows = list(
                        await s.scalars(
                            select(Event)
                            .where(Event.scope == settings.scope, Event.run_id == run_id, Event.id > cursor)
                            .order_by(Event.id)
                            .limit(100)
                        )
                    )
                    run = await get_run(s, run_id)
                    view = snapshot(run)
                if rows or first:
                    if rows:
                        cursor = rows[-1].id
                    yield f"id: {cursor}\nevent: snapshot\ndata: {json.dumps(view)}\n\n"
                    first = False
                else:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/environments", dependencies=[Depends(operator)])
    async def environments():
        async with database.session() as s:
            env = await s.get(Environment, settings.scope)
            return {"owner": env.owner, "generation": env.generation, **env.state} if env else {}

    @app.get("/apps/gh/{environment}/{path:path}", dependencies=[Depends(operator)])
    async def storefront(environment: str, path: str, request: Request):
        if environment not in {"staging", "prod", "canary"}:
            raise HTTPException(404)
        safe = PurePosixPath(path or "index.html")
        if safe.is_absolute() or ".." in safe.parts or any(x.startswith(".") for x in safe.parts):
            raise HTTPException(400, "Invalid asset path")
        async with database.session() as s:
            env = await s.get(Environment, settings.scope)
            if not env:
                raise HTTPException(404, "No storefront release")
            actual = environment
            visitor = request.cookies.get("box-visitor") or uid()
            if (
                environment == "prod"
                and env.state.get("canary")
                and int(hashlib.sha256(visitor.encode()).hexdigest()[:8], 16) % 100
                < env.state.get("weight", 0)
            ):
                actual = "canary"
            sha = env.state.get(actual)
        if not sha:
            raise HTTPException(404, "Environment has no release")
        if settings.mode == "scripted":
            content = "<h1>Scripted storefront release</h1><p>No real repository code is served in scripted mode.</p>"
        else:
            async with database.session() as s:
                asset = await s.get(Asset, (settings.scope, sha, str(safe)))
            if not asset:
                raise HTTPException(404, "Immutable asset is not cached")
            content = asset.content
        mime = mimetypes.guess_type(str(safe))[0] or "application/octet-stream"
        result = Response(
            content,
            media_type=mime,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
                "Content-Security-Policy": "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline' data:; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'",
            },
        )
        result.set_cookie(
            "box-visitor",
            visitor,
            httponly=True,
            samesite="lax",
            secure=settings.public_url.startswith("https"),
        )
        return result

    @app.get("/{path:path}")
    async def ui(path: str):
        root = Path(__file__).resolve().parent.parent / "web" / "dist"
        candidate = (root / path).resolve()
        if root.resolve() not in candidate.parents and candidate != root.resolve():
            raise HTTPException(404)
        if candidate.is_file():
            return FileResponse(candidate)
        if (root / "index.html").exists():
            return FileResponse(root / "index.html")
        return JSONResponse(
            {"name": "box-agent-sdlc", "detail": "Build the console; API is available under /api"}
        )

    return app


app = create_app()
