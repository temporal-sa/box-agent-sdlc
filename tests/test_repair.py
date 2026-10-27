import pytest
from sqlalchemy import select

from box_agent_sdlc.db import Event, Operation, Run, Task, uid
from box_agent_sdlc.repair import resolve_operation
from box_agent_sdlc.state import create_run


async def paused_operation(database):
    async with database.session() as s, s.begin():
        run = await create_run(s, database.settings, {"kind": "issue"})
        run.status, run.state = "paused", {**run.state, "gate": {"key": "intervention"}}
        task = Task(
            id=uid(),
            scope=run.scope,
            run_id=run.id,
            key=run.id + ":uncertain",
            kind="stage",
            status="blocked",
            payload={"stage": "prepare"},
        )
        s.add(task)
        await s.flush()
        operation = Operation(
            id=run.id + "/remote",
            task_id=task.id,
            run_id=run.id,
            request_hash="a" * 64,
            status="ambiguous",
            fence=0,
        )
        s.add(operation)
        return run.id, task.id, operation.id


async def test_verified_remote_result_is_audited_and_resumes_only_its_task(database):
    run_id, task_id, op_id = await paused_operation(database)
    await resolve_operation(
        database,
        run_id=run_id,
        operation_id=op_id,
        request_hash="a" * 64,
        result={"number": 42, "url": "https://github.com/example/repo/issues/42"},
        reason="Verified operation marker on the remote issue",
        actor="presenter",
    )
    async with database.session() as s:
        assert (await s.get(Run, run_id)).status == "running"
        assert (await s.get(Run, run_id)).state["gate"] is None
        assert (await s.get(Task, task_id)).status == "ready"
        assert (await s.get(Operation, op_id)).result["value"]["number"] == 42
        event = await s.scalar(
            select(Event).where(Event.run_id == run_id, Event.kind == "operator_resolution")
        )
        assert event.data["actor"] == "presenter"


async def test_repair_rejects_wrong_fingerprint(database):
    run_id, _, op_id = await paused_operation(database)
    with pytest.raises(ValueError, match="hash"):
        await resolve_operation(
            database,
            run_id=run_id,
            operation_id=op_id,
            request_hash="b" * 64,
            result={},
            reason="Verified remote result with the operator",
            actor="presenter",
        )
    async with database.session() as s:
        assert (await s.get(Run, run_id)).status == "paused"


async def test_repair_cannot_overwrite_already_resolved_outcome(database):
    run_id, task_id, op_id = await paused_operation(database)
    async with database.session() as s, s.begin():
        operation = await s.get(Operation, op_id)
        operation.status, operation.result = "completed", {"value": {"number": 1}}
    with pytest.raises(ValueError, match="already resolved"):
        await resolve_operation(
            database,
            run_id=run_id,
            operation_id=op_id,
            request_hash="a" * 64,
            result={"number": 2},
            reason="Attempt to change an existing result",
            actor="presenter",
        )
