#!/usr/bin/env python3
"""Create/update only the separate sandbox webhook without exposing its secret."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from urllib.parse import urlsplit


def run(command, payload=None):
    result = subprocess.run(
        command,
        input=json.dumps(payload) if payload is not None else None,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            "Webhook setup failed; verify AWS secret access and GitHub repository-admin permission. Provider output was suppressed."
        )
    return json.loads(result.stdout or "null")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="temporal-sa/box-agent-sdlc-storefront")
    parser.add_argument("--url", default="https://box-agent-sdlc.tmprl-demo.cloud/webhooks/github")
    parser.add_argument("--project", default="box-agent-sdlc")
    parser.add_argument("--region", default="us-west-1")
    parser.add_argument("--profile")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        raise RuntimeError("Use a GitHub owner/repository name.")
    if args.repo == "temporal-sa/agentic-sdlc-storefront":
        raise RuntimeError(
            "Use a separate sandbox; the original application's shared storefront is excluded."
        )
    url = urlsplit(args.url)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.path != "/webhooks/github"
        or url.query
        or url.fragment
    ):
        raise RuntimeError("Use the deployed HTTPS /webhooks/github URL.")
    command = ["aws", "--region", args.region, "--no-cli-pager"]
    if args.profile:
        command += ["--profile", args.profile]
    command += [
        "secretsmanager",
        "get-secret-value",
        "--secret-id",
        f"tmprl-dem-cld/{args.project}/runtime",
        "--output",
        "json",
    ]
    secret = json.loads(run(command)["SecretString"])["GITHUB_WEBHOOK_SECRET"]
    pages = run(["gh", "api", f"repos/{args.repo}/hooks", "--paginate", "--slurp"])
    hooks = [hook for page in pages for hook in page]
    matching = [hook for hook in hooks if hook.get("config", {}).get("url") == args.url]
    if len(matching) > 1:
        raise RuntimeError(
            "Multiple matching webhooks exist; remove the duplicate through GitHub before continuing."
        )
    path = f"repos/{args.repo}/hooks"
    method = "POST"
    if matching:
        path += f"/{matching[0]['id']}"
        method = "PATCH"
    payload = {
        "name": "web",
        "active": True,
        "events": [
            "issues",
            "issue_comment",
            "pull_request",
            "pull_request_review",
            "pull_request_review_comment",
        ],
        "config": {"url": args.url, "content_type": "json", "secret": secret, "insecure_ssl": "0"},
    }
    run(["gh", "api", "--method", method, path, "--input", "-"], payload)
    print("Sandbox webhook configured. Signing secret was not printed or passed on a command line.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, KeyError, ValueError, OSError) as error:
        print(
            str(error)
            if isinstance(error, RuntimeError)
            else "Webhook setup failed; no secret values were printed.",
            file=sys.stderr,
        )
        raise SystemExit(1)
