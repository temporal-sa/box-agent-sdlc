"""Explicit SDLC transitions. External work happens in handle; advance only changes PostgreSQL."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm.attributes import flag_modified

from .db import Asset, Comment, Environment, Inbox, Run, Task, db_now, emit, uid
from .engine import TERMINAL, AmbiguousEffect, PermanentError, Retryable, enqueue
from .state import create_run, step

BOT = "<!-- agentic-sdlc-bot -->"
COMMANDS = {"/approve", "/merge", "/ship", "/continue", "/stop", "/skip"}


class Pipeline:
    def __init__(self, db, *, github=None, workspace=None, runtime=None):
        self.db, self.settings = db, db.settings
        self.github, self.workspace, self.runtime = github, workspace, runtime

    async def handle(self, ctx, run, payload):
        if ctx.claim.kind == "webhook":
            return await self.webhook(ctx, payload)
        if ctx.claim.kind == "command":
            return payload
        if ctx.claim.kind == "agent":
            return await self.agent(ctx, run, payload)
        stage = payload["stage"]
        if run.mode == "scripted":
            return await self.scripted_stage(ctx, run, stage, payload)
        from .integrations import GitHubError, HeadChanged, RetryableGitHubError

        try:
            return await self.live_stage(ctx, run, stage, payload)
        except HeadChanged:
            return {"stale": True}
        except RetryableGitHubError as exc:
            raise Retryable(str(exc), after=exc.retry_after) from exc
        except GitHubError as exc:
            raise PermanentError(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 429 or status >= 500 or (status == 403 and "rate" in exc.response.text.lower()):
                raw = exc.response.headers.get("Retry-After", "")
                raise Retryable(f"GitHub HTTP {status}", after=float(raw) if raw.isdigit() else None) from exc
            raise PermanentError(f"GitHub HTTP {status}; fix credentials or input before retrying") from exc

    async def scripted_stage(self, ctx, run, stage, payload):
        # Exercises the same durable transitions without claiming real GitHub/model evidence.
        state = run.state
        if stage == "prepare":
            base = "0" * 40
            return {
                "base_sha": base,
                "base_branch": f"runs/{run.id}/base",
                "head_branch": f"runs/{run.id}/integration",
                "head_sha": base,
                "issue_number": state.get("issue_number") or 1,
                "pr_number": state.get("pr_number"),
                "refs": state.get("refs", []),
            }
        if stage in {"lane_test", "test", "integrated_test"}:
            await ctx.fault("executor")
            return {
                "passed": not (run.kind == "dependency" and stage == "test"),
                "sha": state.get("head_sha", "0" * 40),
                "output": "Scripted executor result",
            }
        if stage == "integrate":
            return {"sha": hashlib.sha1(run.id.encode()).hexdigest(), "conflicts": []}
        if stage == "open_pr":
            return {"number": state.get("pr_number") or 2, "head_sha": state["head_sha"], "url": None}
        if stage == "review_input":
            return {"sha": state["head_sha"], "diff": "Scripted change; no real repository was modified."}
        if stage == "merge":
            return {"sha": state["head_sha"]}
        if stage in {"staging", "canary_check"}:
            fault = await ctx.fault("canary") if stage == "canary_check" else None
            return {
                "healthy": fault != "canary_failure",
                "sha": state.get("merge_sha"),
                "detail": "Scripted health check",
            }
        if stage.startswith("rollback"):
            return {"sha": state.get("merge_sha"), "url": None}
        if stage == "catchup":
            return {"comments": []}
        return {}

    async def read(self, ctx, name, request, call):
        return await ctx.effect(name, request, call, repeat_safe=True)

    async def mutation(self, ctx, name, request, call, reconcile):
        return await ctx.effect(name, request, call, reconcile=reconcile)

    def ws(self, run, branch=None):
        from .integrations import Workspace

        return Workspace(run.state["repo"], branch or run.state["head_branch"])

    async def live_stage(self, ctx, run, stage, payload):
        s, gh = run.state, self.github
        repo = s["repo"]
        op = f"{ctx.claim.scope}/{ctx.claim.key}"
        if stage == "prepare":
            prefix = f"runs/{run.scope}/{run.id.split('.')[-1]}"
            if run.kind == "issue":
                issue_no = s.get("issue_number")
                if not issue_no:
                    issue = await self.mutation(
                        ctx,
                        "issue",
                        {"title": s["title"]},
                        lambda: gh.create_issue(
                            repo,
                            s["title"],
                            s["body"] + "\n\n" + BOT,
                            ["agent", f"agentic-sdlc:{run.scope}"],
                            op_id=op + "/issue",
                        ),
                        lambda: gh.find_issue(repo, op + "/issue"),
                    )
                    issue_no = issue["number"]
                else:
                    issue = await self.read(
                        ctx, "issue", {"number": issue_no}, lambda: gh.get_issue(repo, issue_no)
                    )
                base_ref = os.getenv("GITHUB_BASELINE_REF", "baseline")
                ref = await self.read(ctx, "baseline", {"ref": base_ref}, lambda: gh.get_ref(repo, base_ref))
                base = ref["sha"] if isinstance(ref, dict) else ref
                for name in ["base", "integration"]:
                    await self.mutation(
                        ctx,
                        name,
                        {"branch": f"{prefix}/{name}", "sha": base},
                        lambda name=name: gh.ensure_branch(
                            repo, f"{prefix}/{name}", base, op_id=op + "/" + name
                        ),
                        lambda name=name: gh.get_ref_if_exists(repo, f"{prefix}/{name}"),
                    )
                return {
                    "issue_number": issue_no,
                    "issue_url": issue.get("url", issue.get("html_url")),
                    "refs": [f"issue:{issue_no}"],
                    "base_sha": base,
                    "base_branch": f"{prefix}/base",
                    "head_branch": f"{prefix}/integration",
                    "head_sha": base,
                }
            if not s.get("pr_number"):
                ref = await self.read(
                    ctx,
                    "baseline",
                    {},
                    lambda: gh.get_ref(repo, os.getenv("GITHUB_BASELINE_REF", "baseline")),
                )
                base = ref["sha"] if isinstance(ref, dict) else ref
                branch = f"{prefix}/{'dependency-bump' if run.kind == 'dependency' else 'review-sample'}"
                await self.mutation(
                    ctx,
                    "scenario-branch",
                    {"branch": branch, "sha": base},
                    lambda: gh.ensure_branch(repo, branch, base, op_id=op + "/scenario-branch"),
                    lambda: gh.get_ref_if_exists(repo, branch),
                )
                ws = self.ws(run, branch)
                if run.kind == "dependency":
                    raw = await self.workspace.read_file(ws, "package.json")
                    package = json.loads(raw)
                    package.setdefault("dependencies", {})["orbit-money"] = (
                        "github:temporal-sa/orbit-money#v2.0.0"
                    )
                    await self.read(
                        ctx,
                        "scenario-edit",
                        {"branch": branch},
                        lambda: self.workspace.write_file(
                            ws,
                            "package.json",
                            json.dumps(package, indent=2) + "\n",
                            op_id=op + "/edit",
                            expected_sha=base,
                        ),
                    )
                else:
                    raw = await self.workspace.read_file(ws, "src/cart.js")
                    await self.read(
                        ctx,
                        "scenario-edit",
                        {"branch": branch},
                        lambda: self.workspace.write_file(
                            ws,
                            "src/cart.js",
                            raw.replace("Math.round", "Math.floor"),
                            op_id=op + "/edit",
                            expected_sha=base,
                        ),
                    )
                pr = await self.mutation(
                    ctx,
                    "scenario-pr",
                    {"branch": branch},
                    lambda: gh.open_pull_request(
                        repo,
                        branch,
                        os.getenv("GITHUB_BASELINE_REF", "baseline"),
                        s["title"],
                        s["body"] + "\n" + BOT,
                        op_id=op + "/scenario-pr",
                    ),
                    lambda: gh.find_pull_request(repo, branch, os.getenv("GITHUB_BASELINE_REF", "baseline")),
                )
                await gh.add_labels(repo, pr["number"], [f"agentic-sdlc:{run.scope}"])
                pr = await gh.get_pull_request(repo, pr["number"])
            else:
                pr = await self.read(
                    ctx, "pr", {"number": s["pr_number"]}, lambda: gh.get_pull_request(repo, s["pr_number"])
                )
            return {
                "pr_number": pr["number"],
                "pr_url": pr.get("url"),
                "head_sha": pr["head_sha"],
                "head_branch": pr["head_branch"],
                "base_branch": pr["base_branch"],
                "from_fork": pr.get("from_fork", False),
                "refs": [f"pr:{pr['number']}"],
            }
        if stage == "create_lanes":
            await self.mutation(
                ctx,
                "integration-branch",
                {"branch": s["head_branch"], "sha": s["base_sha"]},
                lambda: gh.ensure_branch(repo, s["head_branch"], s["base_sha"], op_id=op + "/integration"),
                lambda: gh.get_ref_if_exists(repo, s["head_branch"]),
            )
            for lane in s["plan"]["subtasks"]:
                branch = self.lane_branch(run, lane["id"])
                await self.mutation(
                    ctx,
                    "branch-" + lane["id"],
                    {"branch": branch, "sha": s["base_sha"]},
                    lambda branch=branch: gh.ensure_branch(
                        repo, branch, s["base_sha"], op_id=op + "/" + branch
                    ),
                    lambda branch=branch: gh.get_ref_if_exists(repo, branch),
                )
            return {}
        if stage == "prepare_revision":
            await self.mutation(
                ctx,
                "revision-branch",
                {"branch": s["head_branch"], "sha": s["base_sha"]},
                lambda: gh.ensure_branch(repo, s["head_branch"], s["base_sha"], op_id=op + "/revision"),
                lambda: gh.get_ref_if_exists(repo, s["head_branch"]),
            )
            return {}
        if stage in {"test", "lane_test", "integrated_test"}:
            await ctx.fault("executor")
            branch = self.lane_branch(run, payload["lane"]) if payload.get("lane") else s["head_branch"]
            if s.get("from_fork"):
                return {
                    "passed": False,
                    "suggestion_only": True,
                    "sha": s["head_sha"],
                    "output": "Fork code is suggestion-only",
                }
            return await self.read(
                ctx,
                "tests",
                {"branch": branch},
                lambda: self.workspace.run_tests(self.ws(run, branch), op_id=op + "/tests"),
            )
        if stage == "integrate":
            branches = [self.lane_branch(run, x) for x in s["lanes"]]
            return await self.mutation(
                ctx,
                "integrate",
                {"branches": branches},
                lambda: self.workspace.merge_branches(
                    repo, s["head_branch"], s["base_sha"], branches, op_id=op + "/integrate"
                ),
                lambda: self.workspace.find_operation(self.ws(run), op + "/integrate"),
            )
        if stage == "open_pr":
            return await self.mutation(
                ctx,
                "pr",
                {"head": s["head_branch"], "base": s["base_branch"]},
                lambda: gh.open_pull_request(
                    repo,
                    s["head_branch"],
                    s["base_branch"],
                    s["title"],
                    s["plan"]["summary"] + "\n\n" + BOT,
                    op_id=op + "/pr",
                ),
                lambda: gh.find_pull_request(repo, s["head_branch"], s["base_branch"]),
            )
        if stage == "review_input":
            pr = await self.read(ctx, "pr", {}, lambda: gh.get_pull_request(repo, s["pr_number"]))
            diff = await self.read(
                ctx,
                "diff",
                {"sha": pr["head_sha"]},
                lambda: gh.get_diff(repo, pr["base_branch"], pr["head_sha"]),
            )
            await gh.set_status(
                repo, pr["head_sha"], "pending", "Agent review is running", self.settings.public_url
            )
            return {"sha": pr["head_sha"], "diff": diff, "from_fork": pr.get("from_fork", False)}
        if stage == "publish_review":
            review = s["review"]
            result = await self.mutation(
                ctx,
                "review",
                {"sha": s["review_sha"], "review": review},
                lambda: gh.post_review(
                    repo,
                    s["pr_number"],
                    review,
                    "reviewer",
                    op_id=op + "/review",
                    expected_sha=s["review_sha"],
                ),
                lambda: gh.find_review(repo, s["pr_number"], op + "/review"),
            )
            await gh.set_status(
                repo,
                s["review_sha"],
                "success"
                if review["verdict"] == "approve" and s.get("tests_sha") == s["review_sha"]
                else "failure",
                "Review and tests bound to this commit",
                self.settings.public_url,
            )
            return result or {}
        if stage == "explain_dependency":
            diagnosis = s.get("diagnosis", {})
            explanation = "\n\n".join(
                [
                    s["body"],
                    "## Dependency upgrade",
                    s["title"],
                    "## What changed and why it broke",
                    json.dumps(diagnosis, indent=2),
                    "## Repair",
                    s.get("code_summary", "Adapted the caller and validated the final commit."),
                    "## Verification",
                    f"Isolated node:test passed for commit `{s['tests_sha']}`. The independent review follows.",
                ]
            )
            return await self.read(
                ctx,
                "explanation",
                {"sha": s["tests_sha"], "body": explanation},
                lambda: gh.update_pull_request(repo, s["pr_number"], explanation, op_id=op + "/explanation"),
            )
        if stage == "merge":
            pr = await gh.get_pull_request(repo, s["pr_number"])
            if pr["head_sha"] != s["approved_sha"] or s.get("tests_sha") != pr["head_sha"]:
                return {"stale": True}
            return await self.mutation(
                ctx,
                "merge",
                {"sha": s["approved_sha"]},
                lambda: gh.merge_pull_request(
                    repo, s["pr_number"], expected_sha=s["approved_sha"], op_id=op + "/merge"
                ),
                lambda: gh.find_merge(repo, s["pr_number"], s["approved_sha"]),
            )
        if stage in {"staging", "canary_check"}:
            fault = await ctx.fault("canary") if stage == "canary_check" else None
            if fault == "canary_failure":
                return {"healthy": False, "detail": "Injected canary failure"}
            result = await self.read(
                ctx, "probe", {"sha": s["merge_sha"]}, lambda: gh.check_storefront(repo, s["merge_sha"])
            )
            if stage == "staging" and result["healthy"]:
                bundle = await self.read(
                    ctx,
                    "bundle",
                    {"sha": s["merge_sha"]},
                    lambda: self.workspace.bundle_storefront(
                        self.ws(run, s["base_branch"]), op_id=op + "/bundle"
                    ),
                )
                if not bundle.get("passed") or bundle.get("sha") != s["merge_sha"]:
                    return {
                        "healthy": False,
                        "detail": "Isolated storefront bundling failed or its commit changed",
                    }
                result["assets"] = {"index.html": bundle["html"]}
                result["bundle_sha256"] = hashlib.sha256(bundle["html"].encode()).hexdigest()
            return result
        if stage == "post_gate":
            gate = s.get("gate")
            if not gate:
                return {}
            number = (
                s.get("pr_number")
                if gate["key"] in {"merge", "conversation"}
                else s.get("issue_number") or s.get("pr_number")
            )
            await self.mutation(
                ctx,
                "comment",
                {"number": number, "gate": gate},
                lambda: gh.post_comment(
                    repo,
                    number,
                    gate["question"]
                    + "\n\n"
                    + gate.get("context", "")
                    + "\n\n"
                    + " ".join(gate["commands"])
                    + "\n"
                    + BOT,
                    op_id=op + "/comment",
                ),
                lambda: gh.find_comment(repo, number, op + "/comment"),
            )
            return {}
        if stage == "catchup":
            comments = await self.read(
                ctx,
                "comments",
                {"revision": s["plan_revision"]},
                lambda: gh.fetch_comments(repo, issue=s.get("issue_number"), pull=s.get("pr_number")),
            )
            authorized = []
            for c in comments:
                if BOT in c.get("body", ""):
                    continue
                actor = c.get("author") or c.get("user", {}).get("login", "")
                allowed = actor in self.settings.command_actors or await gh.actor_can_approve(repo, actor)
                authorized.append(
                    {
                        "external_id": str(c.get("id")),
                        "author": actor,
                        "body": c.get("body", ""),
                        "authorized": allowed,
                        "url": c.get("url", ""),
                        "created_at": c.get("created_at"),
                    }
                )
            return {"comments": authorized}
        if stage == "close_old_pr":
            await self.read(
                ctx,
                "close",
                {"pr": payload["number"]},
                lambda: gh.close_pull_request(repo, payload["number"], op_id=op + "/close"),
            )
            return {}
        if stage == "rollback_revert":
            branch = s["base_branch"] + "/revert"
            await self.mutation(
                ctx,
                "revert-branch",
                {"branch": branch},
                lambda: gh.ensure_branch(repo, branch, s["merge_sha"], op_id=op + "/branch"),
                lambda: gh.get_ref_if_exists(repo, branch),
            )
            result = await self.mutation(
                ctx,
                "revert",
                {"sha": s["merge_sha"]},
                lambda: self.workspace.revert_merge(
                    self.ws(run, branch), s["merge_sha"], op_id=op + "/revert"
                ),
                lambda: self.workspace.find_operation(self.ws(run, branch), op + "/revert"),
            )
            pr = await self.mutation(
                ctx,
                "revert-pr",
                {"branch": branch},
                lambda: gh.open_pull_request(
                    repo,
                    branch,
                    s["base_branch"],
                    "Revert: " + s["title"],
                    "Failed canary; review this revert.\n" + BOT,
                    op_id=op + "/pr",
                ),
                lambda: gh.find_pull_request(repo, branch, s["base_branch"]),
            )
            return {**result, "url": pr.get("url")}
        if stage == "rollback_reopen":
            await self.read(
                ctx, "reopen", {"issue": s["issue_number"]}, lambda: gh.reopen_issue(repo, s["issue_number"])
            )
            return {}
        return {}

    async def agent(self, ctx, run, payload):
        await ctx.fault("model")
        from .agents import AgentRuntime, ScriptedRuntime
        from .agents.runtime import AgentProtocolError, ProviderRejected, ProviderRetryable
        from .integrations import Workspace

        runtime = self.runtime
        if runtime is None:
            runtime = (
                ScriptedRuntime()
                if run.mode == "scripted"
                else AgentRuntime(workspace_manager=self.workspace, github=self.github, effect=ctx.effect)
            )
        role = payload["role"]
        models = run.state.get("models", {})
        choice = models.get(
            "coder" if role == "coder" else "reviewer" if role in {"reviewer", "security"} else "planner", {}
        )
        if isinstance(choice, str):
            model = choice
        else:
            provider = choice.get(
                "provider", "anthropic" if role not in {"reviewer", "security"} else "openai"
            )
            model = (
                provider
                + ":"
                + choice.get(
                    "model",
                    os.getenv(
                        "DEFAULT_" + provider.upper() + "_MODEL",
                        "claude-sonnet-4-5" if provider == "anthropic" else "gpt-4.1",
                    ),
                )
            )
        checkpoint = payload.get("checkpoint", {})
        workspace = Workspace(
            run.state["repo"], payload.get("branch") or run.state.get("head_branch", "baseline")
        )
        call = lambda: runtime.step(payload["turn_id"], role, payload["input"], checkpoint, workspace, model)

        async def invoke():
            try:
                result = await call()
            except ProviderRetryable as exc:
                raise Retryable(str(exc), after=exc.retry_after) from exc
            except (ProviderRejected, AgentProtocolError) as exc:
                raise PermanentError(str(exc)) from exc
            return (
                dataclasses.asdict(result)
                if dataclasses.is_dataclass(result)
                else result.model_dump(mode="json")
                if hasattr(result, "model_dump")
                else result
            )

        is_model = run.mode == "live" and runtime.next_action(checkpoint) == "model"
        reservation = runtime.reservation_usd(model, payload["input"], checkpoint) if is_model else 0
        if is_model and reservation is None:
            raise AmbiguousEffect(
                "Model pricing is unknown; configure a priced model before starting paid work"
            )
        return await ctx.effect(
            "agent-step",
            {"turn": payload["turn_id"], "index": payload.get("index", 0), "checkpoint": checkpoint},
            invoke,
            repeat_safe=True,
            model=is_model,
            reservation_usd=reservation,
        )

    def lane_branch(self, run, lane):
        return f"runs/{run.scope}/{run.id.split('.')[-1]}/r{run.state['plan_revision']}/{lane}"

    async def stage(self, session, run, name, *, suffix="", delay=0, when=None, **payload):
        state = run.state
        if name == "merge" and not suffix:
            suffix = state.get("approved_sha") or str(state.get("review_round", 0))
        key = f"{run.id}:r{state['plan_revision']}:{name}:{suffix}"
        await enqueue(
            session,
            run.scope,
            run.id,
            key,
            "stage",
            {"stage": name, "input_epoch": [state["plan_revision"], state.get("review_round", 0)], **payload},
            run.version,
            delay=delay,
            when=when,
        )

    async def start_agent(self, session, run, role, after, payload=None, lane=None, suffix=""):
        state = run.state
        turn = f"{run.id}:r{state['plan_revision']}:{role}:{after}:{lane or ''}:{suffix}"
        branch = self.lane_branch(run, lane) if lane else state.get("head_branch")
        history = state.get("agent_histories", {}).get(role + ":" + (branch or ""), [])
        agent_input = deepcopy(payload or {})
        if role == "coder" and state.get("steering"):
            agent_input["feedback"] = (
                (agent_input.get("feedback") or "") + "\nHuman steering:\n" + state["steering"]
            )
        item = {
            "role": role,
            "turn_id": turn,
            "stage": after,
            "lane": lane,
            "input": agent_input,
            "checkpoint": {"messages": history} if history else {},
            "index": 0,
            "branch": branch,
            "steering_revision": state.get("steering_revision", 0),
            "input_epoch": [state["plan_revision"], state.get("review_round", 0)],
            "review_sha": state.get("review_sha"),
        }
        progress_key = (
            f"code:{lane}"
            if lane
            else {
                "clarifier": "clarify",
                "planner": "plan",
                "diagnoser": "diagnose",
                "reviewer": "review",
                "security": "security",
                "triager": "review",
            }.get(role, "build" if run.kind == "dependency" else "integrate")
        )
        item["progress_key"] = progress_key
        await enqueue(session, run.scope, run.id, turn + ":0", "agent", item, run.version)
        state.setdefault("agents", []).append({"role": role, "run_id": turn, "lane": lane})
        step(state, progress_key, "active")
        run.state = state

    async def gate(self, session, run, key, question, commands, context="", deadline=None):
        stamp = await db_now(session)
        state = run.state
        gate_id = uid()
        state["gate"] = {
            "id": gate_id,
            "key": key,
            "question": question,
            "context": context,
            "commands": commands,
            "waiting_since": stamp.timestamp(),
            "closes_at": deadline.timestamp() if deadline else None,
            "head_sha": state.get("approved_sha"),
        }
        run.status = "waiting"
        step(state, key, "waiting", question)
        run.state = state
        await self.stage(session, run, "post_gate", suffix=gate_id)
        await self.stage(session, run, "catchup", suffix=gate_id)
        if deadline:
            await self.stage(session, run, "gate_timeout", suffix=gate_id, when=deadline, gate_id=gate_id)
        await emit(session, run.scope, run.id, "gate_opened", gate=state["gate"])

    async def advance(self, session, task, run, result):
        if task.kind == "webhook":
            return await self.advance_webhook(session, task, result)
        state = deepcopy(run.state)
        run.state = state
        flag_modified(run, "state")
        if task.kind == "command":
            return await self.command(session, task, run, result)
        if task.kind == "agent":
            if task.payload["role"] in {"reviewer", "security"} and task.payload.get(
                "review_sha"
            ) != state.get("review_sha"):
                await emit(
                    session, run.scope, run.id, "obsolete_review_discarded", turn=task.payload["turn_id"]
                )
                return
            usage = result.get("usage", {})
            for k in ("requests", "input_tokens", "output_tokens"):
                state["usage"][k] = state["usage"].get(k, 0) + (usage.get(k) or 0)
            if not result["done"]:
                p = {**task.payload, "checkpoint": result["checkpoint"], "index": task.payload["index"] + 1}
                if p["index"] > 120:
                    raise PermanentError("Agent exceeded 120 durable steps")
                await enqueue(
                    session, run.scope, run.id, p["turn_id"] + f":{p['index']}", "agent", p, run.version
                )
                return
            history_key = task.payload["role"] + ":" + (task.payload.get("branch") or "")
            state.setdefault("agent_histories", {})[history_key] = result["checkpoint"].get("messages", [])
            if task.payload.get("progress_key"):
                step(state, task.payload["progress_key"], "completed")
            return await self.after_agent(session, run, task.payload, result.get("output") or {})
        stage = task.payload["stage"]
        if result.get("stale"):
            state.update(approved_sha=None, security_review=None, gate=None)
            state["review_round"] += 1
            run.status = "running"
            await self.stage(session, run, "integrated_test", suffix=f"stale-{state['review_round']}")
            run.state = state
            return
        if stage == "prepare":
            state.update(result)
            step(state, "prepare", "completed")
            if run.kind == "issue":
                await self.start_agent(session, run, "clarifier", "clarify", self.plan_input(run))
            elif run.kind == "dependency":
                await self.stage(session, run, "test")
            else:
                await self.stage(session, run, "integrated_test", suffix="initial")
        elif stage == "create_lanes":
            for sub in state["plan"]["subtasks"][:3]:
                state["lanes"][sub["id"]] = {"status": "running", "round": 0}
                await self.start_agent(
                    session,
                    run,
                    "coder",
                    "lane_code",
                    {
                        "issue_title": state["title"],
                        "plan_summary": state["plan"]["summary"],
                        "subtask": sub,
                        "feedback": state.get("steering", ""),
                    },
                    lane=sub["id"],
                )
        elif stage == "prepare_revision":
            await self.start_agent(session, run, "planner", "plan", self.plan_input(run))
        elif stage == "lane_test":
            lane = task.payload["lane"]
            item = state["lanes"][lane]
            if result["passed"]:
                item["status"] = "completed"
                step(state, "code:" + lane, "completed", "Tests passed")
                if all(x["status"] == "completed" for x in state["lanes"].values()):
                    step(state, "build", "completed")
                    await self.stage(session, run, "integrate")
            else:
                item["round"] += 1
                if item["round"] >= 3:
                    raise PermanentError("Coder could not pass tests after three rounds")
                sub = next(x for x in state["plan"]["subtasks"] if x["id"] == lane)
                await self.start_agent(
                    session,
                    run,
                    "coder",
                    "lane_code",
                    {"issue_title": state["title"], "subtask": sub, "feedback": result["output"]},
                    lane=lane,
                    suffix=str(item["round"]),
                )
        elif stage == "integrate":
            if result.get("conflicts"):
                await self.start_agent(
                    session,
                    run,
                    "coder",
                    "integrated_code",
                    {
                        "issue_title": state["title"],
                        "feedback": "Resolve integration conflicts: " + str(result["conflicts"]),
                        "subtask": {"description": state["plan"]["summary"]},
                    },
                    suffix="conflicts",
                )
            else:
                state["head_sha"] = result["sha"]
                await self.stage(session, run, "integrated_test", suffix="integrated")
        elif stage in {"test", "integrated_test"}:
            step(
                state,
                "test" if stage == "test" else "integrate",
                "completed" if result["passed"] else "failed",
                result.get("output", ""),
            )
            if result["passed"]:
                state["tests_sha"] = result["sha"]
                state["head_sha"] = result["sha"]
                if stage == "test" and run.kind == "dependency":
                    state["review"] = {
                        "verdict": "approve",
                        "summary": "Compatible dependency upgrade; existing tests pass",
                        "comments": [],
                    }
                    state["review_sha"] = result["sha"]
                    await self.stage(session, run, "publish_review", suffix="compatible")
                elif run.kind == "issue" and not state.get("pr_number"):
                    await self.stage(session, run, "open_pr")
                elif run.kind == "dependency" and not state.get("dependency_explained"):
                    await self.stage(session, run, "explain_dependency", suffix=str(state["review_round"]))
                else:
                    await self.stage(session, run, "review_input", suffix=str(state["review_round"]))
            elif stage == "test":
                state["test_output"] = result.get("output", "")
                await self.start_agent(
                    session,
                    run,
                    "diagnoser",
                    "diagnose",
                    {
                        "package": "orbit-money",
                        "from_version": "1.0",
                        "to_version": "2.0",
                        "test_output": result.get("output"),
                        "diff": state.get("diff", ""),
                    },
                )
            elif state.get("from_fork"):
                await self.stage(session, run, "review_input", suffix="fork")
            else:
                state["review_round"] += 1
                if state["review_round"] > 5:
                    raise PermanentError("Integrated tests failed after five fix rounds")
                await self.start_agent(
                    session,
                    run,
                    "coder",
                    "integrated_code",
                    {
                        "issue_title": state["title"],
                        "subtask": {"description": "Repair integrated tests"},
                        "feedback": result.get("output"),
                    },
                    suffix=str(state["review_round"]),
                )
        elif stage == "open_pr":
            state.update(pr_number=result["number"], pr_url=result.get("url"))
            state["refs"] = list(set(state["refs"] + [f"pr:{result['number']}"]))
            await self.stage(session, run, "review_input", suffix=str(state["review_round"]))
        elif stage == "explain_dependency":
            state["dependency_explained"] = True
            await self.stage(session, run, "review_input", suffix=str(state["review_round"]))
        elif stage == "review_input":
            state.update(
                review_sha=result["sha"],
                head_sha=result["sha"],
                diff=result["diff"],
                from_fork=result.get("from_fork", False),
            )
            await self.start_agent(
                session,
                run,
                "reviewer",
                "review",
                {
                    "issue_title": state["title"],
                    "plan": state.get(
                        "plan", {"summary": state["body"], "acceptance_criteria": [], "subtasks": []}
                    ),
                    "diff": result["diff"],
                },
                suffix=str(state["review_round"]),
            )
        elif stage == "publish_review":
            if state["review"]["verdict"] != "approve":
                if run.kind != "issue" or state.get("from_fork"):
                    await self.conversation_gate(session, run)
                else:
                    await self.fix_review(session, run)
            elif run.kind != "issue":
                await self.conversation_gate(session, run)
            elif run.version == "v2" and not state.get("security_review"):
                await self.start_agent(
                    session,
                    run,
                    "security",
                    "security",
                    {
                        "issue_title": state["title"],
                        "plan": state["plan"],
                        "diff": state["diff"],
                        "focus": "security",
                    },
                    suffix=str(state["review_round"]),
                )
            else:
                state["approved_sha"] = state["review_sha"]
                deadline = (
                    None
                    if state["autonomy"] == "supervised"
                    else await db_now(session) + timedelta(seconds=self.settings.review_window)
                )
                await self.gate(
                    session,
                    run,
                    "merge",
                    "Review passed. Reply /merge to merge this exact commit.",
                    ["/merge", "/stop"],
                    context=state["approved_sha"],
                    deadline=deadline,
                )
        elif stage == "merge":
            if result.get("stale"):
                state.update(approved_sha=None, security_review=None)
                state["review_round"] += 1
                await self.stage(session, run, "integrated_test", suffix=f"stale-{state['review_round']}")
            else:
                state["merge_sha"] = result["sha"]
                step(state, "merge", "completed", result["sha"])
                await self.stage(session, run, "acquire_release", suffix="0", tick=0)
        elif stage == "acquire_release":
            if await self.acquire_environment(session, run):
                state["gate"], run.status = None, "running"
                await self.stage(session, run, "staging")
            else:
                if not state.get("gate"):
                    await self.gate(
                        session,
                        run,
                        "environment",
                        "Another run owns the demo release slot; this run will resume when it is free.",
                        ["/stop"],
                    )
                tick = task.payload.get("tick", 0) + 1
                await self.stage(session, run, "acquire_release", suffix=str(tick), tick=tick, delay=5)
        elif stage == "staging":
            if not result["healthy"]:
                await self.rollback(session, run, "Staging checks failed")
            else:
                for path, content in result.get("assets", {}).items():
                    await session.execute(
                        insert(Asset)
                        .values(scope=run.scope, sha=state["merge_sha"], path=path, content=content)
                        .on_conflict_do_nothing()
                    )
                state["staging_sha"] = state["merge_sha"]
                step(state, "staging", "completed", result.get("detail", ""))
                await self.gate(
                    session,
                    run,
                    "production",
                    "Staging passed. Reply /ship or /stop <reason>.",
                    ["/ship", "/stop"],
                )
        elif stage == "canary_check":
            state["last_health_check"] = (await db_now(session)).isoformat()
            if not result["healthy"]:
                await self.rollback(session, run, result.get("detail", "Canary failed"))
            elif await db_now(session) >= self.parse_time(state["canary_deadline"]):
                await self.release(session, run, promote=True)
                run.status = "completed"
                state["gate"] = None
                step(state, "canary", "completed", "Fresh check passed after durable bake deadline")
                step(state, "promote", "completed", "Demo storefront promoted")
                await emit(session, run.scope, run.id, "promoted", sha=state["merge_sha"])
            else:
                tick = task.payload.get("tick", 0) + 1
                await self.stage(
                    session,
                    run,
                    "canary_check",
                    suffix=str(tick),
                    delay=self.settings.check_seconds,
                    tick=tick,
                )
        elif stage == "rollback_stop":
            await self.release(session, run, promote=False)
            await self.stage(session, run, "rollback_revert")
        elif stage == "rollback_revert":
            state["revert_pr_url"] = result.get("url")
            await self.stage(session, run, "rollback_reopen")
        elif stage == "rollback_reopen":
            run.status = "rolled_back"
            step(state, "canary", "failed", state.get("rollback_reason", ""))
            await emit(session, run.scope, run.id, "rollback_completed", revert_pr=state.get("revert_pr_url"))
        elif stage == "catchup":
            for comment in result.get("comments", []):
                await self.receive_comment(session, run, comment)
        elif stage == "gate_timeout":
            gate = state.get("gate")
            if gate and gate["id"] == task.payload["gate_id"]:
                if gate["key"] == "merge":
                    state["gate"], run.status = None, "running"
                    await self.stage(session, run, "merge")
                elif gate["key"] == "conversation":
                    run.status, state["gate"] = "completed", None
        run.state = deepcopy(state)

    @staticmethod
    def parse_time(value):
        from datetime import datetime

        return datetime.fromisoformat(value)

    def plan_input(self, run):
        s = run.state
        return {
            "issue_title": s["title"],
            "issue_body": s["body"],
            "clarifications": s.get("clarifications"),
            "feedback": s.get("feedback"),
        }

    async def after_agent(self, session, run, payload, output):
        s, stage = run.state, payload["stage"]
        if stage == "clarify":
            questions = output.get("clarification", output).get("questions", [])[:3]
            step(s, "clarify", "completed")
            if questions:
                await self.gate(
                    session,
                    run,
                    "clarify",
                    "Please answer the planner's questions or /skip.",
                    ["/skip", "/stop"],
                    "\n".join(questions),
                )
            else:
                await self.start_agent(session, run, "planner", "plan", self.plan_input(run))
        elif stage == "plan":
            plan = output.get("plan", output)
            subtasks = plan.get("subtasks", [])[:3]
            if (
                not subtasks
                or any(not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,50}", t.get("id", "")) for t in subtasks)
                or len({t["id"] for t in subtasks}) != len(subtasks)
            ):
                raise PermanentError("Planner must return one to three distinct safe subtask IDs")
            s["plan"] = {**plan, "subtasks": subtasks}
            await self.gate(
                session,
                run,
                "plan",
                "Approve this plan with /approve, or reply with changes.",
                ["/approve", "/stop"],
                json.dumps(plan, indent=2),
            )
        elif stage == "lane_code":
            lane = payload["lane"]
            if payload.get("steering_revision", 0) < s.get("steering_revision", 0):
                sub = next(x for x in s["plan"]["subtasks"] if x["id"] == lane)
                await self.start_agent(
                    session,
                    run,
                    "coder",
                    "lane_code",
                    {"issue_title": s["title"], "subtask": sub},
                    lane=lane,
                    suffix=f"steer-{s['steering_revision']}",
                )
            else:
                await self.stage(
                    session,
                    run,
                    "lane_test",
                    suffix=f"{lane}-{s['lanes'][lane]['round']}-{s.get('steering_revision', 0)}",
                    lane=lane,
                )
        elif stage == "integrated_code":
            s["code_summary"] = output.get("summary", "")
            await self.stage(session, run, "integrated_test", suffix=f"fix-{s['review_round']}")
        elif stage == "diagnose":
            s["diagnosis"] = output.get("diagnosis", output)
            step(s, "diagnose", "completed")
            await self.start_agent(
                session,
                run,
                "coder",
                "integrated_code",
                {
                    "issue_title": s["title"],
                    "subtask": {"description": json.dumps(s["diagnosis"])},
                    "feedback": s.get("test_output"),
                },
                suffix="dependency",
            )
        elif stage == "review":
            s["review"] = output.get("review", output)
            step(s, "review", "completed")
            await self.stage(session, run, "publish_review", suffix=str(s["review_round"]))
        elif stage == "security":
            s["security_review"] = output.get("review", output)
            if s["security_review"].get("verdict") != "approve":
                s["review"] = s["security_review"]
                await self.fix_review(session, run)
            else:
                await self.stage(session, run, "publish_review", suffix=f"security-{s['review_round']}")
        elif stage == "triage":
            triage = output.get("triage", output)
            if triage.get("kind") == "scope_change" and run.kind == "issue":
                old = s.get("pr_number")
                if old:
                    await self.stage(session, run, "close_old_pr", suffix=str(old), number=old)
                s["plan_revision"] += 1
                if s["plan_revision"] > 3:
                    raise PermanentError("Scope changed more than three times")
                s.update(
                    feedback=s.get("pending_feedback", ""),
                    pr_number=None,
                    approved_sha=None,
                    security_review=None,
                    lanes={},
                    gate=None,
                    review_round=0,
                )
                s["head_branch"] += f"-r{s['plan_revision']}"
                run.status = "running"
                await self.stage(session, run, "prepare_revision")
            elif s.get("from_fork"):
                await self.conversation_gate(session, run)
            else:
                s["review_round"] += 1
                await self.start_agent(
                    session,
                    run,
                    "coder",
                    "integrated_code",
                    {
                        "issue_title": s["title"],
                        "subtask": {"description": "Address review feedback"},
                        "feedback": s.get("pending_feedback"),
                    },
                    suffix=f"feedback-{s['review_round']}",
                )

    async def fix_review(self, session, run):
        s = run.state
        s["review_round"] += 1
        s["security_review"] = None
        if s["review_round"] > 5:
            raise PermanentError("Review exceeded five fix rounds")
        await self.start_agent(
            session,
            run,
            "coder",
            "integrated_code",
            {
                "issue_title": s["title"],
                "subtask": {"description": "Fix review findings"},
                "feedback": json.dumps(s["review"]),
            },
            suffix=str(s["review_round"]),
        )

    async def conversation_gate(self, session, run):
        await self.gate(
            session,
            run,
            "conversation",
            "Review is published. Reply with feedback; fork PRs receive suggestions only.",
            ["/stop"],
            deadline=await db_now(session) + timedelta(hours=self.settings.idle_hours),
        )

    async def command(self, session, task, run, result):
        s = run.state
        comment = await session.get(Comment, result["comment_id"], with_for_update=True)
        if not comment or comment.handled:
            return
        comment.handled = True
        body = comment.body.strip()
        name = body.split(maxsplit=1)[0].lower() if body else ""
        entry = {
            "id": comment.external_id,
            "author": comment.author,
            "text": body,
            "at": comment.received_at.timestamp(),
            "url": comment.url,
            "status": "addressed",
            "kind": "command" if name.startswith("/") else "feedback",
        }
        s["conversation"].append(entry)
        gate = s.get("gate")
        if name.startswith("/") and not comment.authorized:
            entry.update(status="ignored", outcome="Actor is not authorized for commands")
            await emit(session, run.scope, run.id, "command_rejected", actor=comment.author, command=name)
            return
        if (
            name in {"/approve", "/merge", "/ship", "/skip", "/continue"}
            and gate
            and int(comment.received_at.timestamp()) < int(gate.get("waiting_since", 0))
        ):
            entry.update(status="ignored", outcome="Command predates the current gate")
            return
        if name == "/stop":
            if s.get("merge_sha") and s.get("release_generation"):
                run.status, s["gate"] = "running", None
                await self.rollback(session, run, body)
            else:
                run.status, s["gate"] = "stopped", None
            return
        if name == "/continue" and gate and gate["key"] == "budget":
            run.budget += Decimal("3")
            if run.budget > 25:
                run.budget -= Decimal("3")
                entry.update(status="ignored", outcome="Run budget cap is $25")
                return
            run.status, s["gate"] = "running", None
            await session.execute(
                update(Task)
                .where(Task.run_id == run.id, Task.status == "blocked")
                .values(status="ready", ready_at=await db_now(session))
            )
            return
        if name.startswith("/"):
            if not gate or name not in gate["commands"]:
                entry.update(status="ignored", outcome="Command does not apply to the current gate")
                return
            run.status, s["gate"] = "running", None
            if gate["key"] == "clarify":
                await self.start_agent(session, run, "planner", "plan", self.plan_input(run))
            elif name == "/approve":
                step(s, "plan", "completed", "Approved on GitHub")
                step(s, "build", "active")
                await self.stage(session, run, "create_lanes")
            elif name == "/merge":
                await self.stage(session, run, "merge")
            elif name == "/ship":
                stamp = await db_now(session)
                s["canary_deadline"] = (stamp + timedelta(seconds=self.settings.bake_seconds)).isoformat()
                s["canary_sha"] = s["merge_sha"]
                env = await session.get(Environment, run.scope, with_for_update=True)
                if env.owner != run.id:
                    raise PermanentError("Run no longer owns the release environment")
                env.state = {**env.state, "canary": s["merge_sha"], "weight": 20}
                step(s, "production", "completed")
                step(s, "canary", "active", "20% simulated storefront traffic")
                await self.stage(session, run, "canary_check", suffix="0", tick=0)
            return
        if gate and gate["key"] == "clarify":
            s["clarifications"] = body
            s["gate"], run.status = None, "running"
            await self.start_agent(session, run, "planner", "plan", self.plan_input(run))
        elif gate and gate["key"] == "plan":
            s["feedback"], s["gate"] = body, None
            s["plan_revision"] += 1
            run.status = "running"
            await self.start_agent(session, run, "planner", "plan", self.plan_input(run))
        elif gate and gate["key"] in {"conversation", "merge"}:
            s["gate"], s["pending_feedback"] = None, body
            run.status = "running"
            await self.start_agent(
                session,
                run,
                "triager",
                "triage",
                {
                    "plan": s.get("plan", {"summary": s["body"], "acceptance_criteria": [], "subtasks": []}),
                    "comment": body,
                },
                suffix=comment.id,
            )
        else:
            s["steering"] = s.get("steering", "") + "\n" + body
            s["steering_revision"] = s.get("steering_revision", 0) + 1
            entry.update(status="queued", outcome="Steering queued for the next coder turn")
        run.state = deepcopy(s)

    async def acquire_environment(self, session, run):
        await session.execute(
            insert(Environment).values(scope=run.scope, state={}, generation=0).on_conflict_do_nothing()
        )
        env = await session.get(Environment, run.scope, with_for_update=True)
        if env.owner and env.owner != run.id:
            return False
        env.owner, env.generation = run.id, env.generation + 1
        env.state = {**env.state, "staging": run.state["merge_sha"]}
        run.state["release_generation"] = env.generation
        return True

    async def release(self, session, run, promote):
        env = await session.get(Environment, run.scope, with_for_update=True)
        if not env or env.owner != run.id or env.generation != run.state.get("release_generation"):
            raise PermanentError("Stale release owner cannot promote or compensate")
        state = deepcopy(env.state)
        if promote:
            state["prod"] = run.state["merge_sha"]
        state.update(canary=None, weight=0)
        env.state, env.owner = state, None

    async def rollback(self, session, run, reason):
        run.state.update(gate=None, rollback_reason=reason, compensating=True)
        flag_modified(run, "state")
        await self.stage(session, run, "rollback_stop")
        await emit(session, run.scope, run.id, "rollback_started", reason=reason)

    async def on_failure(self, session, run, reason):
        """Stop owned traffic before terminal failure; compensate only this release epoch."""
        env = await session.get(Environment, run.scope, with_for_update=True)
        if env and env.owner == run.id and env.generation == run.state.get("release_generation"):
            run.status = "running"
            await self.rollback(session, run, reason)
        return run.status

    async def receive_comment(self, session, run, item):
        if BOT in item["body"]:
            return
        comment_id = uid()
        timestamp = (
            self.parse_time(item["created_at"].replace("Z", "+00:00"))
            if item.get("created_at")
            else await db_now(session)
        )
        result = await session.execute(
            insert(Comment)
            .values(
                id=comment_id,
                run_id=run.id,
                external_id=item["external_id"],
                author=item["author"],
                body=item["body"][:8000],
                authorized=item["authorized"],
                url=item.get("url", ""),
                received_at=timestamp,
            )
            .on_conflict_do_nothing()
            .returning(Comment.id)
        )
        inserted = result.scalar_one_or_none()
        if inserted:
            identity = hashlib.sha256(item["external_id"].encode()).hexdigest()[:32]
            await enqueue(
                session,
                run.scope,
                run.id,
                f"{run.id}:comment:{identity}",
                "command",
                {"comment_id": inserted},
                run.version,
            )

    async def webhook(self, ctx, payload):
        async with self.db.session() as session:
            inbox = await session.get(Inbox, payload["inbox_id"])
        p, event = inbox.payload, inbox.event
        if p.get("repository", {}).get("full_name") != self.settings.github_repo:
            return {"ignored": "repository"}
        action = p.get("action")
        item = (
            p.get("issue")
            if event == "issues"
            else p.get("pull_request")
            if event == "pull_request"
            else None
        )
        if item and BOT not in (item.get("body") or ""):
            labels = [x["name"] for x in item.get("labels", [])]
            # Separate baseline identity prevents both demos from acting on the same issue.
            owned = f"agentic-sdlc:{self.settings.scope}" in labels
            if owned and event == "issues" and "agent" in labels and action in {"opened", "labeled"}:
                return {
                    "start": {
                        "kind": "issue",
                        "title": item["title"],
                        "body": item.get("body") or "",
                        "issue_number": item["number"],
                        "issue_url": item.get("html_url"),
                        "refs": [f"issue:{item['number']}"],
                    }
                }
            if (
                owned
                and event == "pull_request"
                and action in {"opened", "reopened", "ready_for_review", "labeled"}
                and not item.get("draft")
            ):
                kind = (
                    "dependency"
                    if "dependencies" in labels or re.match(r"[Bb]ump \S+ from \S+ to \S+", item["title"])
                    else "review"
                )
                return {
                    "start": {
                        "kind": kind,
                        "title": item["title"],
                        "body": item.get("body") or "",
                        "pr_number": item["number"],
                        "pr_url": item.get("html_url"),
                        "refs": [f"pr:{item['number']}"],
                    }
                }
        if event == "pull_request" and action in {"closed", "synchronize"}:
            return {"pr_event": action, "number": p["pull_request"]["number"]}
        raw = p.get("comment") or (p.get("review") if event == "pull_request_review" else None)
        if not raw or action not in {"created", "submitted"} or BOT in (raw.get("body") or ""):
            return {"ignored": "event"}
        number = (p.get("issue") or p.get("pull_request"))["number"]
        ref = f"{'pr' if event != 'issue_comment' or p.get('issue', {}).get('pull_request') else 'issue'}:{number}"
        actor = raw.get("user", {}).get("login", "")
        allowed = actor in self.settings.command_actors or (
            self.settings.mode == "live"
            and await self.github.actor_can_approve(self.settings.github_repo, actor)
        )
        category = {
            "issue_comment": "issue",
            "pull_request_review": "review",
            "pull_request_review_comment": "review-comment",
        }.get(event, event)
        return {
            "comment": {
                "external_id": f"{category}:{raw['id']}",
                "author": actor,
                "body": raw.get("body") or "",
                "authorized": bool(allowed),
                "url": raw.get("html_url", ""),
                "created_at": raw.get("created_at") or raw.get("submitted_at"),
            },
            "ref": ref,
        }

    async def advance_webhook(self, session, task, result):
        inbox = await session.get(Inbox, task.payload["inbox_id"], with_for_update=True)
        if inbox.processed:
            return
        inbox.processed = True
        if result.get("start"):
            request = result["start"]
            number = request.get("issue_number") or request.get("pr_number")
            run_id = f"{task.scope}.gh.{request['kind']}-{number}"
            try:
                await create_run(session, self.settings, request, run_id=run_id)
            except ValueError as exc:
                await emit(session, task.scope, None, "run_rejected", reason=str(exc), request=request)
        if result.get("comment") or result.get("pr_event"):
            runs = list(
                await session.scalars(
                    select(Run).where(Run.scope == task.scope, Run.status.not_in(TERMINAL)).with_for_update()
                )
            )
            for run in runs:
                if result.get("comment") and result["ref"] in run.state.get("refs", []):
                    await self.receive_comment(session, run, result["comment"])
                elif result.get("pr_event") and f"pr:{result['number']}" in run.state.get("refs", []):
                    s = deepcopy(run.state)
                    if result["pr_event"] == "closed":
                        if run.kind != "issue":
                            run.status, s["gate"] = "completed", None
                    else:
                        s.update(approved_sha=None, security_review=None, gate=None)
                        s["review_round"] += 1
                        run.status, run.state = "running", s
                        await self.stage(session, run, "integrated_test", suffix=f"head-{s['review_round']}")
                    run.state = s
