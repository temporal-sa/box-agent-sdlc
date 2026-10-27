"""Real process-death rehearsal with scripted providers; no GitHub or model calls.

Run only against an expendable local database. Evidence explicitly identifies its
scope and does not assert any live-provider or Temporal comparison result.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from box_agent_sdlc.config import Settings
from box_agent_sdlc.db import Database, Event, Fault, Operation, Run, Task
from box_agent_sdlc.state import create_run


async def rehearse(output: Path):
    scope = "rehearsal-" + uuid4().hex[:12]
    settings = Settings(scope=scope, mode="scripted", auth_mode="development")
    database = Database(settings)
    async with database.session() as session, session.begin():
        run = await create_run(session, settings, {"kind": "issue", "title": "Verify worker-loss recovery"})
        run_id = run.id
        session.add(Fault(run_id=run_id, kind="worker_exit", point="after_effect", actor="local-rehearsal"))
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
    env.update(
        APP_SCOPE=scope,
        DATABASE_URL=settings.database_url,
        DEMO_MODE="scripted",
        AUTH_MODE="development",
        PIPELINE_VERSION="v1",
        TASK_LEASE_SECONDS="1",
        HEARTBEAT_SECONDS="0.2",
        POLL_SECONDS="0.05",
    )
    begun = time.monotonic()
    with tempfile.TemporaryFile(mode="w+") as logs:
        first = subprocess.Popen(
            [sys.executable, "-m", "box_agent_sdlc.runner"], env=env, stdout=logs, stderr=logs
        )
        try:
            code = await asyncio.to_thread(first.wait, 20)
            if code != 75:
                raise RuntimeError(f"Injected worker did not exit at its failpoint (exit={code})")
            async with database.session() as session:
                abandoned = list(
                    await session.scalars(select(Task).where(Task.run_id == run_id, Task.status == "leased"))
                )
                assert abandoned, "Process death must leave a lease for another runner to recover"
            second = subprocess.Popen(
                [sys.executable, "-m", "box_agent_sdlc.runner"], env=env, stdout=logs, stderr=logs
            )
            recovered = False
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    async with database.session() as session:
                        run = await session.get(Run, run_id)
                        if run.state.get("gate", {}).get("key") == "plan" if run.state.get("gate") else False:
                            recovered = True
                            break
                    await asyncio.sleep(0.05)
            finally:
                second.terminate()
                await asyncio.to_thread(second.wait, 10)
            if not recovered:
                raise RuntimeError("Replacement runner did not reach the persisted plan gate")
            async with database.session() as session:
                events = list(
                    await session.scalars(select(Event).where(Event.run_id == run_id).order_by(Event.id))
                )
                tasks = list(await session.scalars(select(Task).where(Task.run_id == run_id)))
                faults = list(await session.scalars(select(Fault).where(Fault.run_id == run_id)))
                operations = list(await session.scalars(select(Operation).where(Operation.run_id == run_id)))
            evidence = {
                "mode": "scripted",
                "test": "actual_process_death_after_effect_before_checkpoint",
                "application_scope": scope,
                "run_id": run_id,
                "first_exit_code": code,
                "elapsed_seconds": round(time.monotonic() - begun, 3),
                "reclaimed_tasks": [t.key for t in tasks if t.attempt > 1],
                "fault_consumed_once": len(faults) == 1 and faults[0].consumed_at is not None,
                "reached_gate": "plan",
                "paid_model_calls": 0,
                "github_calls": 0,
                "operations": [{"id": x.id, "status": x.status, "attempts": x.attempts} for x in operations],
                "events": [
                    {"id": e.id, "kind": e.kind, "at": e.at.isoformat(), "data": e.data} for e in events
                ],
                "limitations": "Local PostgreSQL; scripted provider; no AWS, live model, or matched Temporal run.",
            }
            assert evidence["reclaimed_tasks"]
            assert evidence["fault_consumed_once"]
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(evidence, indent=2) + "\n")
            print(
                json.dumps(
                    {
                        k: evidence[k]
                        for k in (
                            "mode",
                            "first_exit_code",
                            "elapsed_seconds",
                            "reached_gate",
                            "fault_consumed_once",
                        )
                    }
                )
            )
        finally:
            if first.poll() is None:
                first.terminate()
                await asyncio.to_thread(first.wait, 10)
    await database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("evidence/local-recovery.json"))
    args = parser.parse_args()
    asyncio.run(rehearse(args.output))
