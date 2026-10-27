"""GitHub effects reconciled by stable operation markers and immutable commit IDs.

The database effect ledger is authoritative. These reconcilers close the unavoidable
window where GitHub accepted an effect but the worker did not record its response.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

BOT_MARKER = "<!-- box-agent-sdlc-bot -->"
REFERENCE_BOT_MARKER = "<!-- agentic-sdlc-bot -->"
INLINE_FALLBACK_NOTE = "GitHub did not accept the inline positions; the findings are listed here instead."
STATUS_CONTEXT = "box-agent-sdlc/agent-review"
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHubError(RuntimeError):
    """A permanent GitHub rejection; messages intentionally exclude response bodies."""


class RetryableGitHubError(GitHubError):
    def __init__(self, status: int, retry_after: float | None = None):
        super().__init__(f"GitHub temporarily unavailable ({status})")
        self.status, self.retry_after = status, retry_after


class HeadChanged(GitHubError):
    """The reviewed/tested SHA no longer matches the pull request head."""


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def operation_marker(op_id: str) -> str:
    return f"<!-- box-op:{hashlib.sha256(op_id.encode()).hexdigest()} -->"


def marked(body: str, op_id: str, agent: str = "Pipeline") -> str:
    return f"{BOT_MARKER}\n{REFERENCE_BOT_MARKER}\n{operation_marker(op_id)}\n**{agent}**\n\n{body}"


def _repository(repo: str) -> str:
    if not REPO.fullmatch(repo):
        raise ValueError("Repository must be owner/name")
    return repo


def _ref(ref: str) -> str:
    if not ref or ref.startswith("-") or ".." in ref or any(c in ref for c in "\x00\n\r\\"):
        raise ValueError("Invalid git reference")
    return quote(ref, safe="/")


class GitHubClient:
    def __init__(self, token: str | None = None, http: httpx.AsyncClient | None = None):
        self._owned = http is None
        self.http = http or httpx.AsyncClient(base_url="https://api.github.com", timeout=30)
        self._headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        self._headers.update({"Cache-Control": "no-cache", "Pragma": "no-cache"})
        self._known_issues: dict[str, tuple[str, dict]] = {}
        self._issue_locks: dict[str, asyncio.Lock] = {}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    async def aclose(self) -> None:
        if self._owned:
            await self.http.aclose()

    async def _request(
        self, method: str, path: str, *, json: Any = None, allow: tuple[int, ...] = ()
    ) -> httpx.Response:
        try:
            response = await self.http.request(method, path, json=json, headers=self._headers)
        except httpx.TransportError as exc:
            raise RetryableGitHubError(503) from exc
        if (
            response.status_code in (408, 429)
            or response.status_code >= 500
            or (response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0")
        ):
            delay = None
            try:
                delay = float(response.headers["retry-after"])
            except (KeyError, ValueError):
                try:
                    delay = max(
                        1, float(response.headers["x-ratelimit-reset"]) - datetime.now(UTC).timestamp()
                    )
                except (KeyError, ValueError):
                    pass
            raise RetryableGitHubError(response.status_code, delay)
        if response.is_error and response.status_code not in allow:
            # Do not log response bodies: GitHub can echo untrusted source or credentials.
            raise GitHubError(f"GitHub rejected {method} {path.split('?')[0]} ({response.status_code})")
        return response

    async def _get(self, path: str) -> Any:
        return (await self._request("GET", path)).json()

    async def _pages(self, path: str) -> list[dict]:
        values: list[dict] = []
        separator = "&" if "?" in path else "?"
        freshness = time.time_ns()
        for page in range(1, 101):
            response = await self._request(
                "GET", f"{path}{separator}per_page=100&page={page}&_box_fresh={freshness}"
            )
            data = response.json()
            if not isinstance(data, list):
                raise GitHubError("Expected a GitHub collection")
            values.extend(data)
            if len(data) < 100:
                return values
        raise GitHubError("GitHub collection exceeds reconciliation limit; manual review required")

    async def get_issue(self, repo: str, number: int) -> dict:
        item = await self._get(f"/repos/{_repository(repo)}/issues/{int(number)}")
        return {**item, "url": item.get("html_url", "")}

    async def get_pull_request(self, repo: str, number: int) -> dict:
        raw = await self._get(f"/repos/{_repository(repo)}/pulls/{int(number)}")
        return {
            "number": raw["number"],
            "url": raw.get("html_url", ""),
            "title": raw["title"],
            "body": raw.get("body") or "",
            "author": (raw.get("user") or {}).get("login", ""),
            "head_branch": raw["head"]["ref"],
            "head_sha": raw["head"]["sha"],
            "base_branch": raw["base"]["ref"],
            "base_sha": raw["base"]["sha"],
            "from_fork": (raw["head"].get("repo") or {}).get("full_name") != repo,
            "state": raw.get("state"),
            "merged": raw.get("merged", False),
            "merge_commit_sha": raw.get("merge_commit_sha"),
        }

    async def get_ref(self, repo: str, branch: str) -> dict:
        repo = _repository(repo)
        response = await self._request("GET", f"/repos/{repo}/git/ref/heads/{_ref(branch)}", allow=(404,))
        if response.status_code == 404:
            response = await self._request("GET", f"/repos/{repo}/git/ref/tags/{_ref(branch)}", allow=(404,))
        if response.status_code == 404:
            if not re.fullmatch(r"[0-9a-f]{40,64}", branch):
                raise GitHubError("Baseline reference does not exist")
            item = await self._get(f"/repos/{repo}/git/commits/{branch}")
            return {"sha": item["sha"], "ref": branch}
        item = response.json()
        for _ in range(3):
            if item["object"].get("type") != "tag":
                break
            tag = await self._get(f"/repos/{repo}/git/tags/{item['object']['sha']}")
            item["object"] = tag["object"]
        if item["object"].get("type") not in {"commit", None}:
            raise GitHubError("Reference does not resolve to a commit")
        return {"sha": item["object"]["sha"], "ref": item.get("ref", f"refs/heads/{branch}")}

    async def get_ref_if_exists(self, repo: str, branch: str) -> dict | None:
        response = await self._request(
            "GET", f"/repos/{_repository(repo)}/git/ref/heads/{_ref(branch)}", allow=(404,)
        )
        return (
            None
            if response.status_code == 404
            else {"sha": response.json()["object"]["sha"], "ref": response.json().get("ref")}
        )

    async def actor_can_approve(self, repo: str, actor: str) -> bool:
        if not actor or actor.endswith("[bot]"):
            return False
        response = await self._request(
            "GET",
            f"/repos/{_repository(repo)}/collaborators/{quote(actor, safe='')}/permission",
            allow=(404,),
        )
        if response.status_code == 404:
            return False
        return response.json().get("permission") in {"admin", "maintain", "write"}

    async def create_issue(self, repo: str, title: str, body: str, labels: list[str], op_id: str) -> dict:
        repo = _repository(repo)
        key = repo + ":" + op_id
        digest = hashlib.sha256(json.dumps([title, body, labels], sort_keys=True).encode()).hexdigest()
        async with self._issue_locks.setdefault(key, asyncio.Lock()):
            known = self._known_issues.get(key)
            if known:
                if known[0] != digest:
                    raise GitHubError("Operation ID reused with a different issue request")
                return dict(known[1])
            marker = operation_marker(op_id)
            for item in await self._pages(f"/repos/{repo}/issues?state=all&sort=created&direction=desc"):
                if "pull_request" not in item and marker in (item.get("body") or ""):
                    result = {"number": item["number"], "url": item.get("html_url", "")}
                    self._known_issues[key] = (digest, result)
                    return result
            item = (
                await self._request(
                    "POST",
                    f"/repos/{repo}/issues",
                    json={"title": title[:200], "body": marked(body, op_id), "labels": labels},
                )
            ).json()
            result = {"number": item["number"], "url": item.get("html_url", "")}
            self._known_issues[key] = (digest, result)
            return result

    async def find_issue(self, repo: str, op_id: str) -> dict | None:
        marker = operation_marker(op_id)
        for item in await self._pages(
            f"/repos/{_repository(repo)}/issues?state=all&sort=created&direction=desc"
        ):
            if "pull_request" not in item and marker in (item.get("body") or ""):
                return {"number": item["number"], "url": item.get("html_url", "")}
        # Absence in an eventually consistent collection is not proof a POST was
        # rejected. The engine treats this inconclusive recovery as ambiguous.
        return None

    async def find_pull_request(self, repo: str, head: str, base: str) -> dict | None:
        repo = _repository(repo)
        for item in await self._pages(
            f"/repos/{repo}/pulls?state=all&head={quote(repo.split('/')[0] + ':' + head, safe='')}&base={quote(base, safe='')}"
        ):
            if item["head"]["ref"] == head and item["base"]["ref"] == base:
                return {
                    "number": item["number"],
                    "url": item.get("html_url", ""),
                    "head_sha": item["head"]["sha"],
                }
        return None

    async def find_comment(self, repo: str, number: int, op_id: str) -> dict | None:
        marker = operation_marker(op_id)
        for item in await self._pages(f"/repos/{_repository(repo)}/issues/{int(number)}/comments"):
            if marker in (item.get("body") or ""):
                return {"id": item["id"], "url": item.get("html_url", "")}
        return None

    async def find_review(self, repo: str, number: int, op_id: str) -> dict | None:
        marker = operation_marker(op_id)
        for item in await self._pages(f"/repos/{_repository(repo)}/pulls/{int(number)}/reviews"):
            if marker in (item.get("body") or ""):
                return {
                    "id": item["id"],
                    "url": item.get("html_url", ""),
                    "inline_fallback": INLINE_FALLBACK_NOTE in (item.get("body") or ""),
                }
        return None

    async def find_merge(self, repo: str, number: int, expected_sha: str) -> dict | None:
        item = await self.assert_head(repo, number, expected_sha)
        return {"sha": item["merge_commit_sha"]} if item["merged"] else None

    async def add_labels(self, repo: str, number: int, labels: list[str]) -> dict:
        await self._request(
            "POST", f"/repos/{_repository(repo)}/issues/{int(number)}/labels", json={"labels": labels}
        )
        return {"labels": labels}

    async def reopen_issue(self, repo: str, number: int) -> dict:
        return (
            await self._request(
                "PATCH", f"/repos/{_repository(repo)}/issues/{int(number)}", json={"state": "open"}
            )
        ).json()

    async def close_issue(self, repo: str, number: int) -> dict:
        return (
            await self._request(
                "PATCH", f"/repos/{_repository(repo)}/issues/{int(number)}", json={"state": "closed"}
            )
        ).json()

    async def ensure_branch(self, repo: str, branch: str, sha: str, op_id: str = "") -> str:
        path = f"/repos/{_repository(repo)}/git/ref/heads/{_ref(branch)}"
        response = await self._request("GET", path, allow=(404,))
        if response.status_code == 200:
            return response.json()["object"]["sha"]
        response = await self._request(
            "POST", f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha}, allow=(422,)
        )
        if response.status_code == 422:
            return (await self.get_ref(repo, branch))["sha"]
        return response.json()["object"]["sha"]

    async def post_comment(
        self, repo: str, number: int, body: str, op_id: str, agent: str = "Pipeline"
    ) -> dict:
        path = f"/repos/{_repository(repo)}/issues/{int(number)}/comments"
        marker = operation_marker(op_id)
        for item in await self._pages(path):
            if marker in (item.get("body") or ""):
                return {"id": item["id"], "url": item.get("html_url", "")}
        item = (await self._request("POST", path, json={"body": marked(body, op_id, agent)})).json()
        return {"id": item["id"], "url": item.get("html_url", "")}

    async def open_pull_request(
        self, repo: str, head: str, base: str, title: str, body: str, op_id: str
    ) -> dict:
        repo = _repository(repo)
        marker = operation_marker(op_id)
        for item in await self._pages(
            f"/repos/{repo}/pulls?state=all&head={quote(repo.split('/')[0] + ':' + head, safe='')}"
        ):
            if marker in (item.get("body") or ""):
                return {
                    "number": item["number"],
                    "url": item.get("html_url", ""),
                    "head_sha": item["head"]["sha"],
                }
        item = (
            await self._request(
                "POST",
                f"/repos/{repo}/pulls",
                json={"title": title[:200], "head": head, "base": base, "body": marked(body, op_id)},
            )
        ).json()
        return {"number": item["number"], "url": item.get("html_url", ""), "head_sha": item["head"]["sha"]}

    async def assert_head(self, repo: str, number: int, expected_sha: str) -> dict:
        pr = await self.get_pull_request(repo, number)
        if pr["head_sha"] != expected_sha:
            raise HeadChanged("Pull request head changed; tests, review and approvals must be renewed")
        return pr

    async def post_review(
        self, repo: str, number: int, review: dict, reviewer: str, op_id: str, expected_sha: str
    ) -> dict:
        await self.assert_head(repo, number, expected_sha)
        path = f"/repos/{_repository(repo)}/pulls/{int(number)}/reviews"
        marker = operation_marker(op_id)
        comments = review.get("comments", [])
        inline = [
            c
            for c in comments
            if isinstance(c.get("file"), str) and c["file"] and type(c.get("line")) is int and c["line"] > 0
        ]
        for item in await self._pages(path):
            if marker in (item.get("body") or ""):
                fallback = INLINE_FALLBACK_NOTE in (item.get("body") or "")
                return {
                    "id": item["id"],
                    "url": item.get("html_url", ""),
                    "inline_posted": bool(inline) and not fallback,
                    "inline_fallback": fallback,
                }
        heading = f"{review.get('verdict', 'request_changes')}: {review.get('summary', '')}"

        def listed(items):
            return "\n".join(
                f"- `{c['file']}`" + (f" line {c['line']}" if c.get("line") else "") + f": {c['comment']}"
                for c in items
            )

        body = heading + "\n\n" + listed([c for c in comments if c not in inline])
        payload = {"body": marked(body, op_id, reviewer), "event": "COMMENT", "commit_id": expected_sha}
        if inline:
            payload["comments"] = [
                {
                    "path": c["file"],
                    "line": c["line"],
                    "side": "RIGHT",
                    "body": f"{BOT_MARKER}\n{REFERENCE_BOT_MARKER}\n{c['comment']}",
                }
                for c in inline
            ]
        response = await self._request("POST", path, json=payload, allow=(422,) if inline else ())
        fallback = response.status_code == 422
        if fallback:
            body = heading + "\n\n" + INLINE_FALLBACK_NOTE + "\n\n" + listed(comments)
            response = await self._request(
                "POST",
                path,
                json={"body": marked(body, op_id, reviewer), "event": "COMMENT", "commit_id": expected_sha},
            )
        item = response.json()
        return {
            "id": item["id"],
            "url": item.get("html_url", ""),
            "inline_posted": bool(inline) and not fallback,
            "inline_fallback": fallback,
        }

    async def set_status(
        self, repo: str, sha: str, state: str, description: str, target_url: str | None = None
    ) -> dict:
        if state not in {"pending", "success", "failure", "error"}:
            raise ValueError("Invalid commit status")
        path = f"/repos/{_repository(repo)}/statuses/{quote(sha, safe='')}"
        for item in await self._pages(path):
            if item.get("context") == STATUS_CONTEXT:
                if item.get("state") == state and item.get("description") == description[:140]:
                    return {"id": item["id"], "sha": sha, "state": state}
                break
        payload = {"state": state, "context": STATUS_CONTEXT, "description": description[:140]}
        if target_url:
            payload["target_url"] = target_url
        item = (await self._request("POST", path, json=payload)).json()
        return {"id": item["id"], "sha": sha, "state": state}

    async def merge_pull_request(self, repo: str, number: int, expected_sha: str, op_id: str = "") -> dict:
        pr = await self.assert_head(repo, number, expected_sha)
        if pr["merged"]:
            return {"sha": pr["merge_commit_sha"]}
        item = (
            await self._request(
                "PUT",
                f"/repos/{_repository(repo)}/pulls/{int(number)}/merge",
                json={"sha": expected_sha, "merge_method": "merge"},
            )
        ).json()
        if not item.get("merged"):
            raise GitHubError("GitHub refused the pull request merge")
        return {"sha": item["sha"]}

    async def check_storefront(self, repo: str, sha: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise ValueError("Storefront probes require an immutable commit SHA")
        failures = []
        for path in ("index.html", "products.json", "src/app.js", "src/cart.js", "styles.css"):
            response = await self._request(
                "GET", f"/repos/{_repository(repo)}/contents/{path}?ref={sha}", allow=(404,)
            )
            if response.status_code != 200 or response.json().get("type") != "file":
                failures.append(path)
        return {
            "healthy": not failures,
            "sha": sha,
            "probes": 5,
            "failed_probes": len(failures),
            "detail": f"{5 - len(failures)}/5 immutable storefront asset probes passed"
            + (f"; missing: {', '.join(failures)}" if failures else ""),
        }

    async def storefront_assets(self, repo: str, sha: str) -> dict[str, str]:
        """Read a bounded immutable static bundle for the token-free web service."""
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise ValueError("Storefront assets require an immutable commit SHA")
        repo = _repository(repo)
        tree = await self._get(f"/repos/{repo}/git/trees/{sha}?recursive=1")
        if tree.get("truncated"):
            raise GitHubError("Storefront tree exceeds the bounded asset limit")
        assets, total = {}, 0
        for entry in tree.get("tree", []):
            path = entry["path"]
            parts = path.split("/")
            if any(
                part.startswith(".") or part in {"tests", "test", "node_modules", "vendor"} for part in parts
            ):
                continue
            if parts[-1] in {"package.json", "package-lock.json", "tsconfig.json"} or path.rsplit(".", 1)[
                -1
            ] not in {"html", "css", "js", "json", "svg"}:
                continue
            if entry.get("type") == "tree":
                continue
            if entry.get("mode") not in {"100644", "100755"} or entry.get("type") != "blob":
                raise GitHubError("Storefront assets cannot contain symlinks or special files")
            if entry.get("size", 0) > 256_000 or len(assets) >= 100:
                raise GitHubError("Storefront assets exceed the bounded file limit")
            blob = await self._get(f"/repos/{repo}/git/blobs/{entry['sha']}")
            try:
                if blob.get("encoding") != "base64":
                    raise ValueError("Unexpected asset encoding")
                body = base64.b64decode(blob["content"].replace("\n", ""), validate=True)
                text = body.decode("utf-8")
            except (KeyError, ValueError, binascii.Error, UnicodeDecodeError) as exc:
                raise GitHubError("Storefront assets must be UTF-8 text") from exc
            total += len(body)
            if len(body) > 256_000 or total > 2_000_000:
                raise GitHubError("Storefront assets exceed the bounded bundle limit")
            if path.startswith("/") or ".." in parts or "\\" in path:
                raise GitHubError("Invalid storefront asset path")
            assets[path] = text
        if "index.html" not in assets:
            raise GitHubError("Storefront has no index.html")
        return assets

    async def close_pull_request(
        self, repo: str, number: int, body: str | None = None, op_id: str | None = None
    ) -> dict:
        if body:
            if not op_id:
                raise ValueError("Closing comment requires an operation ID")
            await self.post_comment(repo, number, body, op_id)
        return (
            await self._request(
                "PATCH", f"/repos/{_repository(repo)}/pulls/{int(number)}", json={"state": "closed"}
            )
        ).json()

    async def update_pull_request(self, repo: str, number: int, body: str, op_id: str) -> dict:
        return (
            await self._request(
                "PATCH",
                f"/repos/{_repository(repo)}/pulls/{int(number)}",
                json={"body": marked(body, op_id, "Coding agent")},
            )
        ).json()

    async def get_diff(self, repo: str, base: str, head: str, max_chars: int = 60_000) -> str:
        item = await self._get(f"/repos/{_repository(repo)}/compare/{_ref(base)}...{_ref(head)}")
        diff = "\n".join(
            f"diff --git a/{f['filename']} b/{f['filename']}\n{f.get('patch', '[binary or unavailable patch]')}"
            for f in item.get("files", [])
        )
        if len(diff) > max_chars:
            raise GitHubError("Diff exceeds review limit; split the change before review")
        return diff

    async def fetch_comments(
        self, repo: str, issue: int | None = None, pull: int | None = None, since: str | None = None
    ) -> list[dict]:
        repo = _repository(repo)
        values: list[dict] = []
        suffix = f"?since={quote(since, safe='')}" if since else ""
        collections = [
            (f"/repos/{repo}/issues/{n}/comments{suffix}", "issue", n)
            for n in dict.fromkeys(n for n in (issue, pull) if n)
        ]
        if pull:
            collections += [
                (f"/repos/{repo}/pulls/{pull}/comments{suffix}", "review-comment", pull),
                (f"/repos/{repo}/pulls/{pull}/reviews", "review", pull),
            ]
        for path, kind, number in collections:
            for raw in await self._pages(path):
                body = raw.get("body") or ""
                created = raw.get("created_at") or raw.get("submitted_at") or ""
                if (
                    not body.strip()
                    or BOT_MARKER in body
                    or REFERENCE_BOT_MARKER in body
                    or (since and created <= since)
                ):
                    continue
                values.append(
                    {
                        "id": f"{kind}:{raw['id']}",
                        "number": number,
                        "author": (raw.get("user") or {}).get("login", ""),
                        "body": (f"(on {raw['path']}) " if raw.get("path") else "") + body[:4000],
                        "url": raw.get("html_url", ""),
                        "created_at": created,
                    }
                )
        return sorted(values, key=lambda c: (c["created_at"], c["id"]))
