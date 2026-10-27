import base64
import hashlib
import hmac
import io
import json
import subprocess
import tarfile

import httpx
import pytest

from box_agent_sdlc.integrations import (
    GitHubClient,
    GitHubError,
    GitWorkspaceManager,
    HeadChanged,
    RetryableGitHubError,
    Workspace,
)
from box_agent_sdlc.integrations.github import BOT_MARKER, operation_marker, verify_signature
from box_agent_sdlc.integrations.workspaces import resolve_path


def client(handler):
    return GitHubClient(
        "test-secret",
        httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler)),
    )


def raw_pr(sha="a" * 40, merged=False):
    return {
        "number": 2,
        "title": "Discount",
        "body": "Body",
        "html_url": "https://github.com/demo/storefront/pull/2",
        "head": {"sha": sha, "ref": "feature", "repo": {"full_name": "demo/storefront"}},
        "base": {"sha": "b" * 40, "ref": "main"},
        "merged": merged,
        "merge_commit_sha": "c" * 40,
    }


def test_webhook_signature_verifies_exact_raw_body():
    body = b'{"action":"opened"}'
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_signature("secret", body, signature)
    assert not verify_signature("secret", body + b" ", signature)
    assert not verify_signature("", body, signature)
    assert not verify_signature("secret", body, None)


async def test_post_comment_reconciles_stable_operation_across_pages():
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        if request.url.params["page"] == "1":
            return httpx.Response(200, json=[{"id": n, "body": "unrelated"} for n in range(100)])
        return httpx.Response(
            200,
            json=[
                {
                    "id": 900,
                    "body": "existing\n" + operation_marker("op-1"),
                    "html_url": "https://github.com/comment/900",
                }
            ],
        )

    result = await client(handle).post_comment("demo/storefront", 1, "Retry this comment", "op-1")
    assert result["id"] == 900 and len(requests) == 2


async def test_issue_creation_reconciles_after_lost_response():
    issues = []
    mutations = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=issues)
        payload = json.loads(request.content)
        mutations.append(payload)
        issues.append({"number": 12, "body": payload["body"], "html_url": "https://github.com/issue/12"})
        return httpx.Response(201, json=issues[0])

    gh = client(handle)
    first = await gh.create_issue("demo/storefront", "Title", "Body", ["agent"], "issue-op")
    second = await gh.create_issue("demo/storefront", "Title", "Body", ["agent"], "issue-op")
    assert first == second and len(mutations) == 1
    assert BOT_MARKER in mutations[0]["body"] and operation_marker("issue-op") in mutations[0]["body"]


async def test_known_issue_response_replay_does_not_depend_on_stale_collection_visibility():
    mutations = []

    def handle(request):
        if request.method == "GET":
            assert request.headers["Cache-Control"] == "no-cache"
            assert "_box_fresh" in request.url.params
            return httpx.Response(200, json=[])
        mutations.append(json.loads(request.content))
        return httpx.Response(201, json={"number": 4, "html_url": "https://github.com/issue/4"})

    gh = client(handle)
    first = await gh.create_issue("demo/storefront", "Title", "Body", [], "stable-issue")
    second = await gh.create_issue("demo/storefront", "Title", "Body", [], "stable-issue")
    assert first == second and len(mutations) == 1
    # A replacement process has no proof yet. Recovery must return an inconclusive
    # result to the engine, which pauses rather than creating another issue.
    assert await client(handle).find_issue("demo/storefront", "stable-issue") is None
    assert len(mutations) == 1


async def test_merge_rejects_stale_head_before_any_mutation():
    requests = []

    def handle(request):
        requests.append(request.method)
        return httpx.Response(200, json=raw_pr())

    with pytest.raises(HeadChanged):
        await client(handle).merge_pull_request("demo/storefront", 2, "z" * 40, "merge-op")
    assert requests == ["GET"]


async def test_merge_supplies_exact_expected_sha_and_reconciles_merged_pr():
    requests = []
    merged = False

    def handle(request):
        nonlocal merged
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=raw_pr(merged=merged))
        assert json.loads(request.content)["sha"] == "a" * 40
        merged = True
        return httpx.Response(200, json={"merged": True, "sha": "c" * 40})

    gh = client(handle)
    assert await gh.merge_pull_request("demo/storefront", 2, "a" * 40, "merge-op") == {"sha": "c" * 40}
    assert await gh.find_merge("demo/storefront", 2, "a" * 40) == {"sha": "c" * 40}
    assert len([r for r in requests if r.method == "PUT"]) == 1


@pytest.mark.parametrize("reject_inline", [False, True])
async def test_review_posts_exact_commit_inline_findings_with_explicit_fallback(reject_inline):
    posts = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=[] if request.url.path.endswith("reviews") else raw_pr())
        body = json.loads(request.content)
        posts.append(body)
        if reject_inline and "comments" in body:
            return httpx.Response(422, json={"message": "Location is outside the diff"})
        return httpx.Response(200, json={"id": 3, "html_url": "https://github.com/review/3"})

    result = await client(handle).post_review(
        "demo/storefront",
        2,
        {
            "verdict": "request_changes",
            "summary": "Incorrect rounding",
            "comments": [
                {"file": "src/cart.js", "line": 5, "comment": "Round fractional cents"},
                {"file": "tests/cart.test.js", "line": None, "comment": "Add a fractional regression"},
            ],
        },
        "Reviewer",
        "review-op",
        "a" * 40,
    )
    assert posts[0]["commit_id"] == "a" * 40
    assert posts[0]["comments"][0]["path"] == "src/cart.js"
    assert posts[0]["comments"][0]["line"] == 5 and posts[0]["comments"][0]["side"] == "RIGHT"
    assert "<!-- agentic-sdlc-bot -->" in posts[0]["comments"][0]["body"]
    assert result["inline_fallback"] is reject_inline
    assert result["inline_posted"] is not reject_inline
    if reject_inline:
        assert len(posts) == 2 and "comments" not in posts[1]
        assert "did not accept the inline positions" in posts[1]["body"]
        assert "Round fractional cents" in posts[1]["body"]


async def test_command_actor_requires_repository_write_permission():
    def handle(request):
        permission = "write" if "/writer/" in request.url.path else "read"
        return httpx.Response(200, json={"permission": permission})

    gh = client(handle)
    assert await gh.actor_can_approve("demo/storefront", "writer")
    assert not await gh.actor_can_approve("demo/storefront", "reader")
    assert not await gh.actor_can_approve("demo/storefront", "automation[bot]")


async def test_rate_limits_preserve_server_retry_after_without_leaking_body():
    gh = client(
        lambda request: httpx.Response(
            429, headers={"Retry-After": "17"}, json={"message": "private server detail"}
        )
    )
    with pytest.raises(RetryableGitHubError) as error:
        await gh.get_issue("demo/storefront", 1)
    assert error.value.retry_after == 17
    assert "private" not in str(error.value)


async def test_baseline_tag_resolves_annotated_tag_to_immutable_commit():
    def handle(request):
        if "/heads/" in request.url.path:
            return httpx.Response(404)
        if "/ref/tags/" in request.url.path:
            return httpx.Response(
                200, json={"ref": "refs/tags/baseline", "object": {"type": "tag", "sha": "t" * 40}}
            )
        return httpx.Response(200, json={"object": {"type": "commit", "sha": "a" * 40}})

    assert await client(handle).get_ref("demo/storefront", "baseline") == {
        "ref": "refs/tags/baseline",
        "sha": "a" * 40,
    }


async def test_storefront_asset_bundle_is_immutable_bounded_and_excludes_private_paths():
    def handle(request):
        if "/trees/" in request.url.path:
            assert request.url.path.endswith("a" * 40)
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"path": "index.html", "mode": "100644", "type": "blob", "sha": "html", "size": 10},
                        {"path": ".env.json", "mode": "100644", "type": "blob", "sha": "secret"},
                        {"path": "tests/cart.test.js", "mode": "100644", "type": "blob", "sha": "tests"},
                        {"path": "package.json", "mode": "100644", "type": "blob", "sha": "package"},
                    ]
                },
            )
        assert request.url.path.endswith("/html")
        return httpx.Response(
            200, json={"encoding": "base64", "content": base64.b64encode(b"<html>ok</html>").decode()}
        )

    assets = await client(handle).storefront_assets("demo/storefront", "a" * 40)
    assert assets == {"index.html": "<html>ok</html>"}


async def test_storefront_asset_bundle_rejects_symlinks_and_truncation():
    for tree in (
        {"truncated": True},
        {"tree": [{"path": "index.html", "mode": "120000", "type": "blob", "sha": "x"}]},
    ):
        gh = client(lambda request, tree=tree: httpx.Response(200, json=tree))
        with pytest.raises(GitHubError):
            await gh.storefront_assets("demo/storefront", "a" * 40)


@pytest.fixture
def git_repo(tmp_path):
    bare, source = tmp_path / "origin.git", tmp_path / "source"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "feature", str(source)], check=True, capture_output=True)

    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.test", *args],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    (source / "src").mkdir()
    (source / "src/cart.js").write_text("export const rate = 1;\n")
    (source / "package.json").write_text('{"type":"module"}')
    git("add", ".")
    git("commit", "-m", "Baseline")
    git("remote", "add", "origin", str(bare))
    git("push", "origin", "feature")
    return bare, source, git


async def test_workspace_edits_reconcile_lost_ack_and_rebuild_on_new_worker(tmp_path, git_repo):
    bare, _source, _git = git_repo
    manager = GitWorkspaceManager(
        tmp_path / "worker1", token="must-not-reach-checkout", remote_override=str(bare)
    )
    ws = Workspace("demo/storefront", "feature")
    before = await manager.head(ws)
    first = await manager.edit_file(
        ws, "src/cart.js", "rate = 1", "rate = 2", "stable-op", expected_sha=before
    )
    second_worker = GitWorkspaceManager(tmp_path / "worker2", remote_override=str(bare))
    retried = await second_worker.edit_file(
        ws, "src/cart.js", "rate = 1", "rate = 2", "stable-op", expected_sha=before
    )
    assert first["sha"] == retried["sha"] and retried["reconciled"]
    assert "rate = 2" in await second_worker.read_file(ws, "src/cart.js")
    config = (second_worker._directory(ws) / ".git/config").read_text()
    assert "must-not-reach-checkout" not in config and "Authorization" not in config
    with pytest.raises(HeadChanged):
        await second_worker.write_file(ws, "src/cart.js", "other", "new-op", expected_sha=before)
    with pytest.raises(ValueError, match="different edit"):
        await second_worker.write_file(ws, "src/cart.js", "other", "stable-op")


def test_workspace_paths_reject_git_traversal_and_symlink(tmp_path):
    (tmp_path / "link").symlink_to("/etc/passwd")
    for path in ("../escape", "/etc/passwd", ".git/config", "link", "src/../../escape"):
        with pytest.raises(ValueError):
            resolve_path(tmp_path, path)


async def test_snapshot_executor_receives_no_git_metadata_or_worker_credentials(tmp_path, git_repo):
    bare, _, _ = git_repo
    seen = []

    def handle(request):
        seen.append(request)
        body = json.loads(request.content)
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(body["snapshot_b64"]))) as archive:
            assert "src/cart.js" in archive.getnames()
            assert not any(".git" in name.split("/") for name in archive.getnames())
        assert body["argv"] == ["node", "--test"]
        assert "worker-secret" not in request.content.decode()
        assert request.headers["Authorization"] == "Bearer executor-secret"
        return httpx.Response(200, json={"exit_code": 0, "output": "passed"})

    manager = GitWorkspaceManager(
        tmp_path / "worker",
        token="worker-secret",
        remote_override=str(bare),
        executor_token="executor-secret",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    result = await manager.run_tests(Workspace("demo/storefront", "feature"), "tests-op")
    assert result["passed"] and len(result["sha"]) == 40 and len(seen) == 1


async def test_integration_result_reconciles_conflict_list(tmp_path, git_repo):
    bare, _, git = git_repo
    baseline = git("rev-parse", "HEAD")
    git("branch", "integration")
    git("push", "origin", "integration")
    git("checkout", "-b", "logic")
    source = git_repo[1]
    (source / "src/cart.js").write_text("export const rate = 2;\n")
    git("commit", "-am", "Logic change")
    git("push", "origin", "logic")
    git("checkout", "-b", "ui", baseline)
    (source / "src/cart.js").write_text("export const rate = 3;\n")
    git("commit", "-am", "Conflicting UI change")
    git("push", "origin", "ui")
    manager = GitWorkspaceManager(tmp_path / "worker", remote_override=str(bare))
    result = await manager.merge_branches(
        "demo/storefront", "integration", baseline, ["logic", "ui"], "integration-op"
    )
    assert result["conflicts"] == ["ui"]
    found = await manager.find_operation(Workspace("demo/storefront", "integration"), "integration-op")
    assert found["sha"] == result["sha"] and found["conflicts"] == ["ui"]
