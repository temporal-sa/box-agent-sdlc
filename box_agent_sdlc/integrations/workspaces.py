"""Disposable git caches; GitHub commits are durable agent workspaces.

Only trusted git operations run here. Repository-authored commands run exclusively in
the credential-free executor service. A database lease/effect ledger must surround
mutations; expected-head pushes prevent stale workers overwriting newer work.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from .github import GitHubError, HeadChanged, _repository

MAX_SNAPSHOT = 5 * 1024 * 1024
ORBIT_MONEY_COMMITS = {
    "v1.0.0": "5a8b894a561a41caace85ca8c53ebbf731b834cf",
    "v2.0.0": "537c1a7d85a3c3bf093e2b6aa22dca6588e9e7eb",
}


@dataclass(frozen=True)
class Workspace:
    repo: str
    branch: str

    def __post_init__(self):
        _repository(self.repo)
        if (
            not self.branch
            or self.branch.startswith("-")
            or ".." in self.branch
            or any(c in self.branch for c in "\n\r\x00\\ :~^?*[")
        ):
            raise ValueError("Invalid branch")


def resolve_path(root: Path, relative: str) -> Path:
    if not relative or "\x00" in relative or len(relative) > 1000:
        raise ValueError("Invalid repository path")
    path = Path(relative)
    if path.is_absolute() or any(p in {"..", ".git"} for p in path.parts):
        raise ValueError("Path must stay inside the repository and outside .git")
    target = root / path
    # Refuse all symlinks, including links that point back into the checkout: their
    # destination can change between validation and access.
    current = root
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Symlink paths are not accessible to agent tools")
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("Path escapes repository")
    return target


class GitWorkspaceManager:
    def __init__(
        self,
        root: str | Path,
        token: str | None = None,
        *,
        executor_url: str = "http://executor:8082",
        executor_token: str = "",
        http: httpx.AsyncClient | None = None,
        remote_override: str | None = None,
    ):
        self.root = Path(root)
        self.token = token
        self.executor_url, self.executor_token = executor_url.rstrip("/"), executor_token
        self.http = http
        # Explicit test injection only; never derive this value from a model argument.
        self.remote_override = remote_override
        self._locks: dict[str, asyncio.Lock] = {}
        self._package_cache: dict[str, dict[str, bytes]] = {}

    def _directory(self, workspace: Workspace) -> Path:
        return self.root / hashlib.sha256(f"{workspace.repo}@{workspace.branch}".encode()).hexdigest()[:24]

    def _lock(self, workspace: Workspace) -> asyncio.Lock:
        return self._locks.setdefault(str(self._directory(workspace)), asyncio.Lock())

    async def _git(self, cwd: Path, *args: str, binary: bool = False) -> str | bytes:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.root),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ALLOW_PROTOCOL": "file" if self.remote_override else "https",
        }
        if self.token and not self.remote_override:
            env.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": "Authorization: Basic "
                    + base64.b64encode(f"x-access-token:{self.token}".encode()).decode(),
                }
            )
        process = await asyncio.create_subprocess_exec(
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, _ = await asyncio.wait_for(process.communicate(), 90)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise GitHubError("Git operation timed out") from None
        if process.returncode:
            # Raw git stderr may contain credential headers, private URLs, or source.
            raise GitHubError(f"Git operation {args[0]} failed (exit {process.returncode})")
        return out if binary else out.decode(errors="replace")

    async def _checkout(self, workspace: Workspace) -> Path:
        directory = self._directory(workspace)
        self.root.mkdir(parents=True, exist_ok=True)
        remote = self.remote_override or f"https://github.com/{workspace.repo}.git"
        if not (directory / ".git").is_dir():
            shutil.rmtree(directory, ignore_errors=True)
            await self._git(
                self.root,
                "clone",
                "--quiet",
                "--single-branch",
                "--branch",
                workspace.branch,
                "--",
                remote,
                str(directory),
            )
        else:
            await self._git(directory, "fetch", "--quiet", "origin", workspace.branch)
            await self._git(directory, "reset", "--hard", f"origin/{workspace.branch}")
            await self._git(directory, "clean", "-fdx")
        return directory

    async def head(self, workspace: Workspace) -> str:
        async with self._lock(workspace):
            root = await self._checkout(workspace)
            return (await self._git(root, "rev-parse", "HEAD")).strip()

    async def list_files(self, workspace: Workspace) -> str:
        async with self._lock(workspace):
            root = await self._checkout(workspace)
            return (await self._git(root, "ls-files"))[:30_000]

    async def read_file(self, workspace: Workspace, path: str) -> str:
        async with self._lock(workspace):
            root = await self._checkout(workspace)
            if path.startswith("node_modules/"):
                await self._prepare_node_dependencies(root)
            target = resolve_path(root, path)
            if not target.is_file():
                return f"No such file: {path}"
            with target.open("rb") as stream:
                return stream.read(30_000).decode(errors="replace")

    async def search_code(self, workspace: Workspace, pattern: str) -> str:
        if len(pattern) > 500:
            raise ValueError("Search pattern too long")
        # Python substring search avoids git's unbounded regex complexity and never
        # interprets model-supplied text as command options.
        async with self._lock(workspace):
            root = await self._checkout(workspace)
            paths = (await self._git(root, "ls-files", "-z")).split("\0")
            output: list[str] = []
            size = 0
            for path in paths[:1000]:
                if not path:
                    continue
                try:
                    target = resolve_path(root, path)
                    if target.stat().st_size > 1_000_000:
                        continue
                    for line, text in enumerate(target.read_text(errors="replace").splitlines(), 1):
                        if pattern in text:
                            found = f"{path}:{line}:{text[:1000]}"
                            output.append(found)
                            size += len(found)
                            if size >= 30_000:
                                return "\n".join(output)[:30_000]
                except (ValueError, OSError):
                    continue
            return "\n".join(output) or "No matches."

    async def _mutate(
        self,
        workspace: Workspace,
        path: str,
        op_id: str,
        content: str | None,
        old_text: str | None = None,
        new_text: str | None = None,
        expected_sha: str | None = None,
    ) -> dict:
        if not op_id:
            raise ValueError("A durable operation ID is required")
        if content is not None and len(content.encode()) > 1_000_000:
            raise ValueError("File exceeds write limit")
        operation = hashlib.sha256(op_id.encode()).hexdigest()
        payload = hashlib.sha256(
            json.dumps([path, content, old_text, new_text], ensure_ascii=False).encode()
        ).hexdigest()
        marker = f"Box-Operation: {operation}"
        async with self._lock(workspace):
            root = await self._checkout(workspace)
            history = await self._git(root, "log", "-1000", "--format=%H%n%B%x00")
            for commit in history.split("\0"):
                if marker in commit:
                    if f"Box-Payload: {payload}" not in commit:
                        raise ValueError("Operation ID reused with a different edit")
                    return {"sha": commit.strip().splitlines()[0], "path": path, "reconciled": True}
            before = (await self._git(root, "rev-parse", "HEAD")).strip()
            if expected_sha and before != expected_sha:
                raise HeadChanged("Workspace head changed")
            target = resolve_path(root, path)
            if content is None:
                text = target.read_text()
                if not old_text or text.count(old_text) != 1:
                    raise ValueError("old_text must match exactly once")
                content = text.replace(old_text, new_text or "", 1)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            await self._git(root, "add", "--", path)
            changed = await self._git(root, "diff", "--cached", "--name-only")
            if not changed.strip():
                return {"sha": before, "path": path, "unchanged": True}
            message = f"Agent edit: {path}\n\n{marker}\nBox-Payload: {payload}"
            await self._git(
                root,
                "-c",
                "user.name=Box coding agent",
                "-c",
                "user.email=box-agent@users.noreply.github.com",
                "commit",
                "--quiet",
                "-m",
                message,
            )
            after = (await self._git(root, "rev-parse", "HEAD")).strip()
            await self._git(
                root,
                "push",
                "--quiet",
                f"--force-with-lease=refs/heads/{workspace.branch}:{before}",
                "origin",
                f"HEAD:refs/heads/{workspace.branch}",
            )
            return {"sha": after, "path": path, "reconciled": False}

    async def write_file(
        self, workspace: Workspace, path: str, content: str, op_id: str, expected_sha: str | None = None
    ) -> dict:
        return await self._mutate(workspace, path, op_id, content, expected_sha=expected_sha)

    async def edit_file(
        self,
        workspace: Workspace,
        path: str,
        old_text: str,
        new_text: str,
        op_id: str,
        expected_sha: str | None = None,
    ) -> dict:
        return await self._mutate(workspace, path, op_id, None, old_text, new_text, expected_sha)

    async def snapshot(self, workspace: Workspace) -> tuple[str, str]:
        async with self._lock(workspace):
            root = await self._checkout(workspace)
            sha = (await self._git(root, "rev-parse", "HEAD")).strip()
            data = await self._git(root, "archive", "--format=tar", "HEAD", binary=True)
            dependencies = await self._prepare_node_dependencies(root)
            if dependencies:
                buffer = io.BytesIO(data)
                with tarfile.open(fileobj=buffer, mode="a") as archive:
                    for path in sorted((root / "node_modules" / "orbit-money").iterdir()):
                        payload = path.read_bytes()
                        item = tarfile.TarInfo("node_modules/orbit-money/" + path.name)
                        item.size, item.mode, item.mtime = len(payload), 0o644, 0
                        archive.addfile(item, io.BytesIO(payload))
                data = buffer.getvalue()
            if len(data) > MAX_SNAPSHOT:
                raise ValueError("Repository snapshot exceeds executor size limit")
            return sha, base64.b64encode(data).decode()

    async def run_command(self, workspace: Workspace, command: str, op_id: str) -> dict:
        if len(command) > 4000:
            raise ValueError("Command too long")
        return await self.execute(workspace, ["/bin/sh", "-c", command], op_id)

    async def run_tests(self, workspace: Workspace, op_id: str) -> dict:
        # Node 22 discovers *.test.js recursively with no path argument; supplying a
        # directory is interpreted as a module on that supported runtime.
        return await self.execute(workspace, ["node", "--test"], op_id)

    async def _prepare_node_dependencies(self, root: Path) -> dict:
        manifest_path = resolve_path(root, "package.json")
        if not manifest_path.exists():
            return {}
        manifest = json.loads(manifest_path.read_text())
        dependencies = manifest.get("dependencies") or {}
        if not dependencies:
            return {}
        if set(dependencies) != {"orbit-money"}:
            raise ValueError(
                "Only the pinned orbit-money demo dependency is supported; vendor other dependencies explicitly"
            )
        spec = dependencies["orbit-money"]
        prefix = "github:temporal-sa/orbit-money#"
        if (
            not isinstance(spec, str)
            or not spec.startswith(prefix)
            or spec[len(prefix) :] not in ORBIT_MONEY_COMMITS
        ):
            raise ValueError("orbit-money must use the approved pinned v1.0.0 or v2.0.0 demo version")
        sha = ORBIT_MONEY_COMMITS[spec[len(prefix) :]]
        if sha not in self._package_cache:
            own = self.http is None
            http = self.http or httpx.AsyncClient(timeout=30)
            try:
                files = {}
                for name in ("package.json", "index.js", "CHANGELOG.md", "README.md", "LICENSE"):
                    response = await http.get(
                        f"https://raw.githubusercontent.com/temporal-sa/orbit-money/{sha}/{name}"
                    )
                    response.raise_for_status()
                    if len(response.content) > 100_000:
                        raise ValueError("Pinned demo package file exceeds size limit")
                    files[name] = response.content
                package = json.loads(files["package.json"])
                # Scripts remain inert metadata. Runtime dependencies require a
                # separate explicitly reviewed supply-chain preparation path.
                if package.get("dependencies"):
                    raise ValueError("Pinned package unexpectedly declares runtime dependencies")
                self._package_cache[sha] = files
            finally:
                if own:
                    await http.aclose()
        target = resolve_path(root, "node_modules/orbit-money")
        target.mkdir(parents=True, exist_ok=True)
        for name, content in self._package_cache[sha].items():
            resolve_path(root, f"node_modules/orbit-money/{name}").write_bytes(content)
        return {"orbit-money": {"sha": sha, "spec": spec}}

    async def prepare_node_dependencies(self, workspace: Workspace) -> dict:
        async with self._lock(workspace):
            return await self._prepare_node_dependencies(await self._checkout(workspace))

    async def find_operation(self, workspace: Workspace, op_id: str) -> dict | None:
        marker = "Box-Operation: " + hashlib.sha256(op_id.encode()).hexdigest()
        async with self._lock(workspace):
            root = await self._checkout(workspace)
            history = await self._git(root, "log", "-1000", "--format=%H%n%B%x00")
            for commit in history.split("\0"):
                if marker in commit:
                    result = {"sha": commit.strip().splitlines()[0], "reconciled": True, "conflicts": []}
                    for line in commit.splitlines():
                        if line.startswith("Box-Merge-Result: "):
                            result["conflicts"] = json.loads(line.partition(": ")[2])
                    return result
        return None

    async def execute(self, workspace: Workspace, argv: list[str], op_id: str) -> dict:
        sha, archive = await self.snapshot(workspace)
        if not self.executor_token:
            raise RuntimeError("Executor token is required")
        own_http = self.http is None
        client = self.http or httpx.AsyncClient(timeout=110)
        try:
            response = await client.post(
                f"{self.executor_url}/execute",
                headers={"Authorization": f"Bearer {self.executor_token}"},
                json={"operation_id": op_id, "snapshot_b64": archive, "argv": argv, "timeout_seconds": 90},
            )
            response.raise_for_status()
            return {**response.json(), "sha": sha, "passed": response.json().get("exit_code") == 0}
        finally:
            if own_http:
                await client.aclose()

    async def bundle_storefront(self, workspace: Workspace, op_id: str) -> dict:
        """Build self-contained HTML in the isolated executor, never on the worker."""
        sha, archive = await self.snapshot(workspace)
        if not self.executor_token:
            raise RuntimeError("Executor token is required")
        own_http = self.http is None
        client = self.http or httpx.AsyncClient(timeout=110)
        try:
            response = await client.post(
                f"{self.executor_url}/bundle",
                headers={"Authorization": f"Bearer {self.executor_token}"},
                json={"operation_id": op_id, "snapshot_b64": archive, "timeout_seconds": 90},
            )
            response.raise_for_status()
            result = response.json()
            return {
                **result,
                "sha": sha,
                "passed": bool(result.get("passed"))
                and result.get("exit_code") == 0
                and isinstance(result.get("html"), str),
            }
        finally:
            if own_http:
                await client.aclose()

    async def merge_branches(
        self, repo: str, integration_branch: str, base_sha: str, branches: list[str], op_id: str
    ) -> dict:
        """Merge existing branches without executing repository hooks; report conflicts."""
        workspace = Workspace(repo, integration_branch)
        async with self._lock(workspace):
            root = await self._checkout(workspace)
            before = (await self._git(root, "rev-parse", "HEAD")).strip()
            conflicts: list[str] = []
            for branch in branches:
                Workspace(repo, branch)
                await self._git(root, "fetch", "--quiet", "origin", branch)
                try:
                    await self._git(
                        root,
                        "-c",
                        "user.name=Box integration agent",
                        "-c",
                        "user.email=box-agent@users.noreply.github.com",
                        "merge",
                        "--no-ff",
                        "-m",
                        f"Integrate {branch}",
                        "FETCH_HEAD",
                    )
                except GitHubError:
                    await self._git(root, "merge", "--abort")
                    conflicts.append(branch)
            await self._git(
                root,
                "-c",
                "user.name=Box integration agent",
                "-c",
                "user.email=box-agent@users.noreply.github.com",
                "commit",
                "--allow-empty",
                "--quiet",
                "-m",
                f"Record integration result\n\nBox-Operation: {hashlib.sha256(op_id.encode()).hexdigest()}\nBox-Merge-Result: {json.dumps(conflicts)}",
            )
            sha = (await self._git(root, "rev-parse", "HEAD")).strip()
            if sha != before:
                await self._git(
                    root,
                    "push",
                    "--quiet",
                    f"--force-with-lease=refs/heads/{integration_branch}:{before}",
                    "origin",
                    f"HEAD:refs/heads/{integration_branch}",
                )
            return {"sha": sha, "conflicts": conflicts}

    async def revert_merge(self, workspace: Workspace, merge_sha: str, op_id: str) -> dict:
        marker = "Box-Operation: " + hashlib.sha256(op_id.encode()).hexdigest()
        async with self._lock(workspace):
            root = await self._checkout(workspace)
            history = await self._git(root, "log", "-1000", "--format=%H%n%B%x00")
            for commit in history.split("\0"):
                if marker in commit:
                    return {"sha": commit.strip().splitlines()[0], "reconciled": True}
            before = (await self._git(root, "rev-parse", "HEAD")).strip()
            if not re.fullmatch(r"[0-9a-f]{40,64}", merge_sha):
                raise ValueError("Invalid merge SHA")
            await self._git(root, "revert", "--no-commit", "-m", "1", merge_sha)
            await self._git(
                root,
                "-c",
                "user.name=Box release agent",
                "-c",
                "user.email=box-agent@users.noreply.github.com",
                "commit",
                "--quiet",
                "-m",
                f"Revert failed canary {merge_sha}\n\n{marker}",
            )
            sha = (await self._git(root, "rev-parse", "HEAD")).strip()
            await self._git(
                root,
                "push",
                "--quiet",
                f"--force-with-lease=refs/heads/{workspace.branch}:{before}",
                "origin",
                f"HEAD:refs/heads/{workspace.branch}",
            )
            return {"sha": sha, "reconciled": False}
