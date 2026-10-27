"""Record a verified remote result; never infer that an uncertain action did not occur."""

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from box_agent_sdlc.config import Settings
from box_agent_sdlc.db import Database, Operation, Run
from box_agent_sdlc.repair import resolve_operation


async def main(args):
    db = Database(Settings())
    try:
        if args.list:
            async with db.session() as session:
                run = await session.get(Run, args.run_id)
                if not run or run.scope != db.settings.scope:
                    raise ValueError("Run is outside this application scope")
                rows = list(
                    await session.scalars(
                        select(Operation).where(Operation.run_id == run.id, Operation.status != "completed")
                    )
                )
                print(
                    json.dumps(
                        [
                            {"id": row.id, "request_hash": row.request_hash, "status": row.status}
                            for row in rows
                        ],
                        indent=2,
                    )
                )
        else:
            if not all((args.operation_id, args.request_hash, args.result, args.reason, args.actor)):
                raise ValueError(
                    "Resolution requires operation, hash, verified result file, reason, and actor"
                )
            await resolve_operation(
                db,
                run_id=args.run_id,
                operation_id=args.operation_id,
                request_hash=args.request_hash,
                result=json.loads(args.result.read_text()),
                reason=args.reason,
                actor=args.actor,
            )
            print("Recorded verified result and resumed its task; audit event retained.")
    finally:
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--operation-id")
    parser.add_argument("--request-hash")
    parser.add_argument("--result", type=Path)
    parser.add_argument("--reason")
    parser.add_argument("--actor")
    asyncio.run(main(parser.parse_args()))
