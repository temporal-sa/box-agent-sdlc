"""Serializable run contracts shared by the API, runner, and console."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select

from .db import Run, Scope, db_now, emit, uid
from .engine import ACTIVE, enqueue, ensure_scope

STEPS = {
    "issue": [
        ("prepare", "GitHub issue"),
        ("clarify", "Clarification"),
        ("plan", "Plan approval"),
        ("build", "Parallel coding"),
        ("integrate", "Integration and tests"),
        ("review", "Review"),
        ("merge", "Merge approval"),
        ("staging", "Staging"),
        ("production", "Release approval"),
        ("canary", "Canary checks"),
        ("promote", "Promotion"),
    ],
    "dependency": [
        ("prepare", "Dependency PR"),
        ("test", "Test upgrade"),
        ("diagnose", "Diagnosis"),
        ("build", "Repair"),
        ("review", "Independent review"),
        ("conversation", "PR conversation"),
    ],
    "review": [("prepare", "Pull request"), ("review", "Review"), ("conversation", "PR conversation")],
}


async def create_run(session, settings, request: dict, *, run_id=None):
    await ensure_scope(session, settings.scope)
    scope = await session.get(Scope, settings.scope, with_for_update=True)
    run_id = run_id or f"{settings.scope}.scenario-{uid()[:12]}"
    existing = await session.get(Run, run_id)
    if existing:
        return existing
    count = await session.scalar(
        select(func.count())
        .select_from(Run)
        .where(Run.scope == settings.scope, Run.status.in_(ACTIVE | {"paused"}))
    )
    if count >= settings.max_runs:
        raise ValueError(f"This application already has {settings.max_runs} active runs")
    kind = request.get("kind", "issue")
    if kind not in STEPS:
        raise ValueError("Unknown scenario kind")
    budget = Decimal(str(request.get("budget_usd", settings.default_budget)))
    if not 0 < budget <= 25:
        raise ValueError("Budget must be greater than zero and at most $25")
    repo = request.get("repo") or settings.github_repo
    if repo != settings.github_repo:
        raise ValueError("Repository is outside this application's allowlist")
    stamp = await db_now(session)
    state = {
        "title": request.get("title")
        or {
            "issue": "Add free shipping above $75",
            "dependency": "Bump orbit-money from 1.0 to 2.0",
            "review": "Review a discount rounding change",
        }[kind],
        "body": request.get("body", "Implement the requirement with tests and explain the change."),
        "repo": repo,
        "models": settings.agent_models(request.get("models", {})),
        "autonomy": request.get("autonomy", "supervised"),
        "steps": [{"key": k, "label": label, "status": "pending", "attempt": 0} for k, label in STEPS[kind]],
        "gate": None,
        "conversation": [],
        "agents": [],
        "lanes": {},
        "plan_revision": 1,
        "usage": {"cost_usd": 0, "input_tokens": 0, "output_tokens": 0, "requests": 0},
        "issue_number": request.get("issue_number"),
        "pr_number": request.get("pr_number"),
        "issue_url": request.get("issue_url"),
        "pr_url": request.get("pr_url"),
        "refs": request.get("refs", []),
        "review_round": 0,
    }
    run = Run(
        id=run_id,
        scope=settings.scope,
        kind=kind,
        version=scope.version,
        mode=settings.mode,
        budget=budget,
        state=state,
        deadline=stamp + timedelta(days=3),
    )
    session.add(run)
    await session.flush()
    await enqueue(
        session,
        settings.scope,
        run.id,
        f"{run.id}:r1:prepare",
        "stage",
        {"stage": "prepare"},
        version=run.version,
    )
    await emit(session, settings.scope, run.id, "run_created", kind=kind, version=run.version, mode=run.mode)
    return run


def snapshot(run: Run):
    public = deepcopy(run.state)
    public.pop("agent_histories", None)
    return {
        **public,
        "run_id": run.id,
        "kind": run.kind,
        "version": run.version,
        "mode": run.mode,
        "status": run.status,
        "revision": run.revision,
        "budget_usd": float(run.budget),
        "reserved_usd": float(run.reserved),
        "usage": {**run.state.get("usage", {}), "cost_usd": float(run.spent)},
        "started_at": run.created_at.timestamp(),
        "updated_at": run.updated_at.timestamp(),
        "deadline": run.deadline.timestamp(),
    }


def step(state: dict, key: str, status: str, detail=""):
    steps = state.setdefault("steps", [])
    item = next((x for x in steps if x["key"] == key), None)
    if not item:
        item = {"key": key, "label": key, "attempt": 0}
        steps.append(item)
    item.update(status=status, detail=detail)
    if status == "active":
        item["attempt"] = item.get("attempt", 0) + 1
