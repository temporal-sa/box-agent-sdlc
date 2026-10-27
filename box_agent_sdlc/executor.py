"""Credential-free, fail-closed Linux execution service (port 8082).

Each request gets a disposable snapshot and a fresh bubblewrap PID, user, mount and
network namespace. Install bubblewrap in the dedicated executor image; do not run
this service inside the API/worker container or give it a cloud role.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import io
import os
import shutil
import signal
import sys
import tarfile
import tempfile
import time
from collections import OrderedDict
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

MAX_SNAPSHOT_BYTES = int(os.getenv("EXECUTOR_MAX_SNAPSHOT_BYTES", str(5 * 1024 * 1024)))
MAX_OUTPUT_BYTES = int(os.getenv("EXECUTOR_MAX_OUTPUT_BYTES", "30000"))
MAX_TIMEOUT_SECONDS = min(120, int(os.getenv("EXECUTOR_TIMEOUT_SECONDS", "90")))
MAX_REQUEST_BYTES = MAX_SNAPSHOT_BYTES * 4 // 3 + 100_000
MAX_CONCURRENCY = int(os.getenv("EXECUTOR_MAX_CONCURRENCY", "2"))
BUNDLER_SCRIPT = "/usr/local/lib/box-bundler/bundler.mjs"
BUNDLE_OUTPUT = ".box-bundle-output.html"
MAX_BUNDLE_BYTES = 2_000_000


class ExecuteRequest(BaseModel):
    operation_id: str = Field(min_length=1, max_length=250)
    snapshot_b64: str = Field(max_length=MAX_SNAPSHOT_BYTES * 4 // 3 + 4)
    argv: list[str] = Field(min_length=1, max_length=30)
    timeout_seconds: int = Field(default=MAX_TIMEOUT_SECONDS, ge=1, le=MAX_TIMEOUT_SECONDS)


class BundleRequest(BaseModel):
    operation_id: str = Field(min_length=1, max_length=250)
    snapshot_b64: str = Field(max_length=MAX_SNAPSHOT_BYTES * 4 // 3 + 4)
    timeout_seconds: int = Field(default=MAX_TIMEOUT_SECONDS, ge=1, le=MAX_TIMEOUT_SECONDS)


def extract_snapshot(encoded: str, destination: Path) -> None:
    """Extract regular files only, without tarfile's path/link side effects."""
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Snapshot must be valid base64") from exc
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Snapshot exceeds size limit")
    count = total = 0
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for item in archive:
                count += 1
                path = PurePosixPath(item.name)
                if (
                    count > 2000
                    or len(item.name) > 1000
                    or path.is_absolute()
                    or any(p in {"..", ".git", ".aws", ".ssh"} for p in path.parts)
                    or "\\" in item.name
                    or "\x00" in item.name
                ):
                    raise ValueError("Unsafe snapshot entry")
                if not item.isdir() and not item.isreg():
                    raise ValueError("Snapshots may only contain regular files and directories")
                target = destination.joinpath(*path.parts)
                if not target.resolve().is_relative_to(destination.resolve()):
                    raise ValueError("Snapshot path escapes workspace")
                if item.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if item.name in seen:
                    raise ValueError("Duplicate snapshot entry")
                seen.add(item.name)
                total += item.size
                if item.size > 2 * 1024 * 1024 or total > MAX_SNAPSHOT_BYTES:
                    raise ValueError("Expanded snapshot exceeds size limit")
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(item)
                if stream is None:
                    raise ValueError("Missing snapshot file")
                with target.open("xb") as output:
                    shutil.copyfileobj(stream, output, length=64 * 1024)
                target.chmod(0o644)
    except (tarfile.TarError, OSError) as exc:
        raise ValueError("Malformed snapshot archive") from exc


def sandbox_command(workspace: Path, argv: list[str]) -> list[str]:
    if sys.platform != "linux" or not shutil.which("bwrap"):
        raise RuntimeError("Linux bubblewrap is required; execution is disabled")
    if not argv or any(len(a) > 8000 or "\x00" in a for a in argv):
        raise ValueError("Invalid command arguments")
    if argv[0] not in {"/bin/sh", "sh", "node", "npm", "python3", "/bin/true"}:
        raise ValueError("Command must use an allowed interpreter")
    command = [
        shutil.which("bwrap"),
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/local/bin:/usr/bin:/bin",
        "--setenv",
        "HOME",
        "/tmp/home",
        "--setenv",
        "LD_LIBRARY_PATH",
        "/usr/local/lib",
        "--setenv",
        "CI",
        "1",
        "--setenv",
        "NODE_OPTIONS",
        "",
        "--setenv",
        "npm_config_cache",
        "/tmp/npm-cache",
        "--tmpfs",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/run",
        "--dir",
        "/tmp/home",
        "--dir",
        "/etc",
        "--dir",
        "/home",
        "--dir",
        "/sys",
    ]
    for path in ("/usr", "/bin", "/lib", "/lib64"):
        if Path(path).exists():
            command += ["--ro-bind", path, path]
    # No procfs is mounted at all: /proc and /sys are empty. Besides hiding every
    # process environment and kernel control, this works under the registry's
    # restricted outer container proc mount policy without added capabilities.
    command += ["--bind", str(workspace), "/workspace", "--chdir", "/workspace", "--", *argv]
    return command


def _limits(timeout: int) -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (timeout + 1, timeout + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_NPROC, (128, 128))


async def run_isolated(request: ExecuteRequest, directory: Path) -> dict:
    command = sandbox_command(directory, request.argv)
    started = time.monotonic()
    # The launcher environment contains no inherited credentials. Memory is bounded
    # by the executor container cgroup, rather than RLIMIT_AS (Node reserves huge VA).
    process = await asyncio.create_subprocess_exec(
        *command,
        env={"PATH": "/usr/bin:/bin"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
        preexec_fn=lambda: _limits(request.timeout_seconds),
    )
    captured = bytearray()
    truncated = False

    async def drain():
        nonlocal truncated
        while chunk := await process.stdout.read(8192):
            captured.extend(chunk)
            if len(captured) > MAX_OUTPUT_BYTES:
                del captured[: len(captured) - MAX_OUTPUT_BYTES]
                truncated = True

    reader = asyncio.create_task(drain())
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), request.timeout_seconds)
    except TimeoutError:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
    finally:
        await reader
    # Invalid UTF-8 must not expand a bounded byte buffer into an unbounded result.
    output = captured.decode(errors="replace").encode()
    if len(output) > MAX_OUTPUT_BYTES:
        output, truncated = output[-MAX_OUTPUT_BYTES:], True
    return {
        "exit_code": 124 if timed_out else process.returncode,
        "output": output.decode(errors="ignore"),
        "truncated": truncated,
        "timed_out": timed_out,
        "duration_seconds": round(time.monotonic() - started, 3),
        "sandbox": "bubblewrap",
        "network": "disabled",
    }


app = FastAPI(title="Box SDLC isolated executor", docs_url=None, redoc_url=None, openapi_url=None)
_semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
_results: OrderedDict[str, tuple[str, dict]] = OrderedDict()
_bundle_results: OrderedDict[str, tuple[str, dict]] = OrderedDict()
_ready: tuple[float, bool] = (0, False)


@app.get("/healthz")
async def healthz():
    # The registry uses /healthz for its executor probe, so process liveness alone
    # must never make an executor with unavailable containment appear deployable.
    await readyz()
    return {"ok": True, "service": "executor", "sandbox": "bubblewrap"}


@app.get("/readyz")
async def readyz():
    global _ready
    if time.monotonic() - _ready[0] > 30:
        try:
            with tempfile.TemporaryDirectory(prefix="executor-probe-") as directory:
                result = await run_isolated(
                    ExecuteRequest(
                        operation_id="readiness",
                        snapshot_b64="",
                        argv=["node", BUNDLER_SCRIPT, "--self-test"],
                        timeout_seconds=5,
                    ),
                    Path(directory),
                )
            _ready = (time.monotonic(), result["exit_code"] == 0)
        except (RuntimeError, OSError):
            _ready = (time.monotonic(), False)
    if not _ready[1]:
        raise HTTPException(503, "Isolated execution unavailable: bubblewrap/user namespaces required")
    return {"ok": True, "sandbox": "bubblewrap"}


async def authenticated_body(request: Request) -> bytearray:
    expected = os.getenv("EXECUTOR_TOKEN", "")
    actual = request.headers.get("authorization", "")
    if not expected or not hmac.compare_digest(actual, f"Bearer {expected}"):
        raise HTTPException(401, "Executor authentication required")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_REQUEST_BYTES:
            raise HTTPException(413, "Executor request exceeds size limit")
    return body


@app.post("/execute")
async def execute(request: Request):
    body = await authenticated_body(request)
    try:
        job = ExecuteRequest.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(422, "Invalid executor request") from exc
    digest = hashlib.sha256(body).hexdigest()
    cached = _results.get(job.operation_id)
    if cached:
        if cached[0] != digest:
            raise HTTPException(409, "Operation ID reused with different input")
        return cached[1]
    async with _semaphore:
        try:
            with tempfile.TemporaryDirectory(prefix="box-execute-") as directory:
                root = Path(directory)
                extract_snapshot(job.snapshot_b64, root)
                result = await run_isolated(job, root)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        _results[job.operation_id] = (digest, result)
        _results.move_to_end(job.operation_id)
        while len(_results) > 200:
            _results.popitem(last=False)
        return result


@app.post("/bundle")
async def bundle_storefront(request: Request):
    body = await authenticated_body(request)
    try:
        job = BundleRequest.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(422, "Invalid bundler request") from exc
    digest = hashlib.sha256(body).hexdigest()
    cached = _bundle_results.get(job.operation_id)
    if cached:
        if cached[0] != digest:
            raise HTTPException(409, "Operation ID reused with different input")
        return cached[1]
    async with _semaphore:
        try:
            with tempfile.TemporaryDirectory(prefix="box-bundle-") as directory:
                root = Path(directory)
                extract_snapshot(job.snapshot_b64, root)
                output = root / BUNDLE_OUTPUT
                if output.exists():
                    if not output.is_file():
                        raise ValueError("Reserved bundler output path is not a regular file")
                    output.unlink()
                result = await run_isolated(
                    ExecuteRequest(
                        operation_id=job.operation_id,
                        snapshot_b64="",
                        argv=["node", BUNDLER_SCRIPT],
                        timeout_seconds=job.timeout_seconds,
                    ),
                    root,
                )
                result["passed"] = result["exit_code"] == 0 and not result["timed_out"]
                result["html"] = None
                if result["passed"]:
                    if not output.is_file() or output.is_symlink():
                        raise ValueError("Bundler did not produce a regular HTML file")
                    with output.open("rb") as stream:
                        html = stream.read(MAX_BUNDLE_BYTES + 1)
                    if len(html) > MAX_BUNDLE_BYTES:
                        raise ValueError("Self-contained storefront exceeds the 2 MB limit")
                    try:
                        result["html"] = html.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise ValueError("Bundled storefront must be UTF-8") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        _bundle_results[job.operation_id] = (digest, result)
        _bundle_results.move_to_end(job.operation_id)
        while len(_bundle_results) > 8:
            _bundle_results.popitem(last=False)
        return result


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("EXECUTOR_PORT", "8082")), access_log=False)
