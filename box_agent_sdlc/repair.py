"""Audited operator repair after verifying an uncertain remote outcome."""

from copy import deepcopy
from decimal import Decimal

from .db import Operation, Run, Task, db_now, emit
from .engine import obsolete


async def resolve_operation(db, *, run_id, operation_id, request_hash, result, reason, actor):
    if not isinstance(result, (dict, list, str, int, float, bool, type(None))):
        raise ValueError("Resolution must be JSON")
    if len(reason.strip()) < 10:
        raise ValueError("Record the evidence used to verify the remote outcome")
    async with db.session() as session, session.begin():
        operation = await session.get(Operation, operation_id, with_for_update=True)
        if not operation or operation.run_id != run_id or operation.request_hash != request_hash:
            raise ValueError("Operation identity or request hash does not match")
        task = await session.get(Task, operation.task_id, with_for_update=True)
        run = await session.get(Run, run_id, with_for_update=True)
        if run.scope != db.settings.scope or run.status != "paused" or task.status != "blocked":
            raise ValueError("Only a paused operation in this scope can be resolved")
        if operation.status not in {"started", "ambiguous"} or obsolete(task, run):
            raise ValueError("Operation is already resolved or belongs to superseded work")
        operation.status, operation.result = "completed", {"value": result}
        if operation.reservation:
            run.reserved -= operation.reservation
            run.spent += operation.reservation
            await emit(
                session,
                run.scope,
                run.id,
                "uncertain_model_spend",
                amount=float(operation.reservation),
                operation=operation.id,
            )
            operation.reservation = Decimal(0)
        task.status, task.error, task.owner, task.lease_until = "ready", None, None, None
        task.fence += 1
        task.ready_at = await db_now(session)
        state = deepcopy(run.state)
        state["gate"] = None
        run.state, run.status = state, "running"
        await emit(
            session,
            run.scope,
            run.id,
            "operator_resolution",
            operation=operation.id,
            request_hash=request_hash,
            reason=reason,
            actor=actor,
        )
