#!/usr/bin/env python3
"""Rehearse real GitHub effects only in the dedicated private sandbox.

Credentials are captured from gh into process memory. They are never printed,
written to evidence, placed in command arguments, or supplied to generated code.
No model calls, AWS calls, or application deployments are performed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from box_agent_sdlc.integrations import (
    GitHubClient,
    GitHubError,
    GitWorkspaceManager,
    RetryableGitHubError,
    Workspace,
)

SANDBOX = "temporal-sa/box-agent-sdlc-storefront"


def cli_token():
    result = subprocess.run(["gh", "auth", "token"], text=True, capture_output=True, check=False)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError(
            "GitHub CLI authentication is unavailable; run gh auth login through the normal account workflow."
        )
    return result.stdout.strip()


def save_evidence(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")


async def await_visible(read):
    """Bounded read-only wait for GitHub's collections to expose a known effect."""
    for attempt in range(20):
        try:
            value = await read()
        except RetryableGitHubError:
            value = None
        if value is not None:
            return value
        if attempt < 19:
            await asyncio.sleep(0.5)
    raise RuntimeError(
        "Known GitHub effect did not become visible through a fresh client; no mutation was repeated."
    )


async def rehearse(args):
    root = Path(__file__).resolve().parents[1]
    evidence = {
        "schema_version": 1,
        "kind": "github_adapter_rehearsal",
        "repository": SANDBOX,
        "run_id": args.run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "calls": {"model": 0, "aws": 0, "application_deployments": 0},
        "note": "Real GitHub adapter integration only. No LLM or AWS calls. This is not a Temporal/PostgreSQL comparative durability result.",
    }
    output = args.evidence.resolve() if args.evidence else root / "evidence" / "local-github.json"
    audit = root / "evidence" / f"github-{args.run_id}.json"
    stage = "authentication"
    github = None
    fresh = None
    try:
        token = cli_token()
        github = GitHubClient(token=token)
        fresh = GitHubClient(token=token)
        stage = "repository_guard"
        metadata = await github._get(f"/repos/{SANDBOX}")
        if metadata.get("full_name") != SANDBOX or not metadata.get("private"):
            raise ValueError("The rehearsal requires the dedicated private sandbox.")
        protected = {name: (await github.get_ref(SANDBOX, name))["sha"] for name in ("main", "baseline")}
        evidence["protected_refs_before"] = protected
        base, head = f"rehearsal/{args.run_id}/base", f"rehearsal/{args.run_id}/head"
        evidence["branches"] = {"base": base, "head": head}
        operation = f"github-rehearsal/{args.run_id}"

        stage = "issue"
        issue = await github.create_issue(
            SANDBOX,
            f"Adapter rehearsal {args.run_id}",
            "Automated adapter integration rehearsal only. No agent action, model call, or AWS deployment is requested.",
            [],
            operation + "/issue",
        )
        evidence["issue"] = {**issue, "same_client_replay": False, "fresh_client_lookup_verified": False}
        issue_again = await github.create_issue(
            SANDBOX,
            f"Adapter rehearsal {args.run_id}",
            "Automated adapter integration rehearsal only. No agent action, model call, or AWS deployment is requested.",
            [],
            operation + "/issue",
        )
        evidence["issue"]["replay_number"] = issue_again["number"]
        assert issue_again["number"] == issue["number"], "Issue replay created another issue"
        found_issue = await await_visible(lambda: fresh.find_issue(SANDBOX, operation + "/issue"))
        assert found_issue["number"] == issue["number"], "Fresh issue lookup found another issue"
        evidence["issue"].update(
            replay_same_number=True, same_client_replay=True, fresh_client_lookup_verified=True
        )

        stage = "branches"
        for branch in (base, head):
            await github.ensure_branch(
                SANDBOX, branch, protected["baseline"], operation + "/" + branch.rsplit("/", 1)[-1]
            )
        with tempfile.TemporaryDirectory(prefix="box-github-rehearsal-") as directory:
            manager = GitWorkspaceManager(directory, token=token)
            workspace = Workspace(SANDBOX, head)
            proof = (
                f"Box SDLC GitHub adapter rehearsal\nRun: {args.run_id}\nNo model calls or AWS deployment.\n"
            )
            stage = "write_reconciliation"
            written = await manager.write_file(
                workspace, "proof.txt", proof, operation + "/proof", expected_sha=protected["baseline"]
            )
            repeated = await manager.write_file(
                workspace, "proof.txt", proof, operation + "/proof", expected_sha=protected["baseline"]
            )
            assert written["sha"] == repeated["sha"], "File write replay created another commit"
            assert repeated.get("reconciled"), "File write replay was not reconciled"
            head_sha = written["sha"]
            evidence["write"] = {
                "sha": head_sha,
                "replay_sha": repeated["sha"],
                "first_reconciled": written.get("reconciled", False),
                "replay_reconciled": True,
            }

        stage = "pull_request"
        body = f"Adapter rehearsal for #{issue['number']}. Merge is restricted to `{base}`. No model or AWS calls."
        pr = await github.open_pull_request(
            SANDBOX, head, base, f"Adapter rehearsal {args.run_id}", body, operation + "/pull"
        )
        evidence["pull_request"] = {**pr, "head_sha": head_sha, "base": base}
        pr_again = await github.open_pull_request(
            SANDBOX, head, base, f"Adapter rehearsal {args.run_id}", body, operation + "/pull"
        )
        assert pr["number"] == pr_again["number"], "PR replay created another pull request"
        found_pr = await await_visible(lambda: fresh.find_pull_request(SANDBOX, head, base))
        assert found_pr["number"] == pr["number"], "Fresh PR lookup found another pull request"
        checked = await github.assert_head(SANDBOX, pr["number"], head_sha)
        assert checked["base_branch"] == base and checked["head_branch"] == head, (
            "PR branches escaped the rehearsal"
        )
        evidence["pull_request"].update(replay_same_number=True, fresh_client_lookup_verified=True)

        stage = "comment"
        comment_body = "Automated adapter reconciliation rehearsal. This comment is not an agent finding or deployment result."
        comment = await github.post_comment(
            SANDBOX, pr["number"], comment_body, operation + "/comment", "Rehearsal"
        )
        evidence["comment"] = dict(comment)
        comment_again = await github.post_comment(
            SANDBOX, pr["number"], comment_body, operation + "/comment", "Rehearsal"
        )
        assert comment["id"] == comment_again["id"], "Comment replay created another comment"
        found_comment = await await_visible(
            lambda: fresh.find_comment(SANDBOX, pr["number"], operation + "/comment")
        )
        assert found_comment["id"] == comment["id"], "Fresh comment lookup found another comment"
        evidence["comment"].update(replay_same_id=True, fresh_client_lookup_verified=True)

        stage = "exact_sha_review"
        review_body = {
            "verdict": "comment",
            "summary": "Adapter rehearsal on the exact proof-file commit; no code-quality or comparative claim.",
            "comments": [
                {
                    "file": "proof.txt",
                    "line": 1,
                    "comment": "Rehearsal proof line tied to the tested immutable SHA.",
                }
            ],
        }
        review = await github.post_review(
            SANDBOX, pr["number"], review_body, "Rehearsal reviewer", operation + "/review", head_sha
        )
        evidence["review"] = {**review, "sha": head_sha}
        review_again = await github.post_review(
            SANDBOX, pr["number"], review_body, "Rehearsal reviewer", operation + "/review", head_sha
        )
        assert review["id"] == review_again["id"], "Review replay created another review"
        found_review = await await_visible(
            lambda: fresh.find_review(SANDBOX, pr["number"], operation + "/review")
        )
        assert found_review["id"] == review["id"], "Fresh review lookup found another review"
        evidence["review"].update(replay_same_id=True, fresh_client_lookup_verified=True)

        stage = "exact_sha_status"
        description = "Adapter rehearsal only; no model or AWS calls"
        status = await github.set_status(SANDBOX, head_sha, "success", description, pr["url"])
        status_again = await github.set_status(SANDBOX, head_sha, "success", description, pr["url"])
        assert status["id"] == status_again["id"], "Commit status replay created another status"
        evidence["commit_status"] = {**status, "replay_same_id": True}

        stage = "merge"
        # GitHub may briefly return null while computing a new PR's mergeability.
        # This performs only reads; no remote mutation is retried blindly.
        for _ in range(10):
            metadata = await github._get(f"/repos/{SANDBOX}/pulls/{pr['number']}")
            if metadata.get("merged") or metadata.get("mergeable") is not None:
                break
            await asyncio.sleep(1)
        merged = await github.merge_pull_request(SANDBOX, pr["number"], head_sha, operation + "/merge")
        merged_again = await github.merge_pull_request(SANDBOX, pr["number"], head_sha, operation + "/merge")
        assert merged["sha"] == merged_again["sha"], "Merge replay created another merge"
        found_merge = await await_visible(lambda: fresh.find_merge(SANDBOX, pr["number"], head_sha))
        assert found_merge["sha"] == merged["sha"], "Fresh merge lookup found another merge"
        assert (await github.get_ref(SANDBOX, base))["sha"] == merged["sha"], (
            "Own base branch does not point to the merge"
        )
        evidence["merge"] = {
            "sha": merged["sha"],
            "expected_head_sha": head_sha,
            "base": base,
            "replay_same_sha": True,
            "fresh_client_lookup_verified": True,
        }

        stage = "close_issue"
        closed = await github.close_issue(SANDBOX, issue["number"])
        assert closed["state"] == "closed", "Rehearsal issue was not closed"
        evidence["issue"]["closed"] = True
        after = {name: (await github.get_ref(SANDBOX, name))["sha"] for name in ("main", "baseline")}
        assert after == protected, "Protected fixture branches changed during the rehearsal"
        evidence["protected_refs_after"] = after
        evidence["protected_refs_unchanged"] = True
        evidence["status"] = "passed"
    except Exception as error:
        evidence["status"] = "failed"
        evidence["failed_stage"] = stage
        evidence["error_type"] = type(error).__name__
        # GitHubError is deliberately sanitized at the adapter boundary.
        if isinstance(error, (GitHubError, AssertionError, ValueError)):
            evidence["error"] = str(error)
    finally:
        if github is not None:
            await github.aclose()
        if fresh is not None:
            await fresh.aclose()
        evidence["finished_at"] = datetime.now(UTC).isoformat()
        save_evidence(audit, evidence)
        save_evidence(output, evidence)
    print(
        json.dumps(
            {
                "status": evidence["status"],
                "stage": evidence.get("failed_stage"),
                "evidence": str(output),
                "audit": str(audit),
                "issue_url": evidence.get("issue", {}).get("url"),
                "pull_request_url": evidence.get("pull_request", {}).get("url"),
            },
            indent=2,
        )
    )
    return 0 if evidence["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id", default=datetime.now(UTC).strftime("%Y%m%dT%H%M%S").lower() + "-" + secrets.token_hex(3)
    )
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,50}", args.run_id):
        parser.error("Use a run ID with 3–51 lowercase letters, digits, or hyphens.")
    raise SystemExit(asyncio.run(rehearse(args)))


if __name__ == "__main__":
    main()
