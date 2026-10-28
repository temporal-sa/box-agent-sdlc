# Build your own agent from the Agentic SDLC reference, without Temporal

Reuse the reference's data contracts, prompts, repository tools and GitHub integration patterns. Build a separate execution layer to store progress and recover unfinished work. Several useful definitions live inside modules that import Temporal, so extract those definitions rather than importing the original modules unchanged.

This guide uses direct Pydantic AI agents with a PostgreSQL-backed runner. FastAPI and the React console are optional application interfaces. Start with one useful agent, then add the larger delivery pipeline if your use case needs it.

All source links point to revision **`10967999c9a9a43a97d8353b23dfea4aa7810913`** of [temporal-sa/temporal-demo-agentic-sdlc](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/tree/10967999c9a9a43a97d8353b23dfea4aa7810913). They describe that revision, rather than whichever files happen to be on `main` later.

**1. Make a separate project and keep the reference pinned.**

```sh
git clone https://github.com/temporal-sa/temporal-demo-agentic-sdlc.git agentic-sdlc-reference
git -C agentic-sdlc-reference checkout --detach 10967999c9a9a43a97d8353b23dfea4aa7810913
mkdir -p my-agent/my_agent
```

Keep `agentic-sdlc-reference` for reading and create your application under `my-agent`. Record the source revision and which definitions you copied. Start a fresh dependency manifest and lockfile. Retain compatible Python, FastAPI, Pydantic and Pydantic AI/provider versions from the reference as a starting point; remove `temporalio`, `temporal-agent-harness`, and the `temporal` Pydantic AI extra. Add SQLAlchemy, psycopg and Alembic for the execution layer below. Redis is not required for correctness in this design.

**2. Copy the contracts and extract the agent definitions.**

| Original file | What to reuse | What to change |
| --- | --- | --- |
| [`agentic_sdlc/models.py`](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/10967999c9a9a43a97d8353b23dfea4aa7810913/agentic_sdlc/models.py) | This is the best file to copy first. It contains plain Pydantic contracts: `Workspace`, `ModelChoice`, `Plan`, `Subtask`, `Clarification`, `Triage`, `Review`, `ReviewComment`, `Diagnosis`, and their request/reply types. | Keep only the types your agent needs. Change domain fields and model defaults. Rename runtime-specific fields such as `agent_workflow_id` if retaining the UI contracts. These models describe data; they do not persist it. |
| [`agentic_sdlc/agents/runtime.py`](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/10967999c9a9a43a97d8353b23dfea4aa7810913/agentic_sdlc/agents/runtime.py) | Extract `CoderOutput` and the `PLANNER_INSTRUCTIONS`, `CODER_INSTRUCTIONS`, `DIAGNOSE_INSTRUCTIONS`, `REVIEWER_INSTRUCTIONS`, and `SECURITY_INSTRUCTIONS` strings. Put prompts in your own `prompts.py`. | Replace `_agent()`, `TemporalAgent`, `MODEL_ACTIVITY`, and the harness toolset wiring. Importing the original module also constructs provider clients at import time. |
| [`agentic_sdlc/agents/workflows.py`](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/10967999c9a9a43a97d8353b23dfea4aa7810913/agentic_sdlc/agents/workflows.py) | Extract `CLARIFY_INSTRUCTIONS` and `TRIAGE_INSTRUCTIONS`. Reuse prompt assembly from `PlannerAgent.clarify/plan/triage`, `CoderAgent.code/diagnose`, and `ReviewerAgent.review`. Retain the request-limit policy when useful. | Replace agent decorators, `AgentWorkflowRunner`, `RedisEventStream`, and `_AgentBase`. Store the message history that `_history` currently holds in your database. |
| [`agentic_sdlc/agents/registry.py`](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/10967999c9a9a43a97d8353b23dfea4aa7810913/agentic_sdlc/agents/registry.py) | Adapt `allowlist()`, `is_allowed()`, `_build()`, and `settings_for()` for provider selection and settings. | `build_registry()` wraps models in the harness's `DemoModel`; return ordinary provider models instead. Verify the chosen model is available and priced. The source disables SDK retries, so your runner must implement retry scheduling. |

Construct ordinary Pydantic AI agents with the extracted instructions, output models and your own dependency context. Keep the model client, tool execution and checkpointing behind an `AgentRuntime` interface so the rest of your application does not depend on one agent framework's execution objects.

For a new domain, change the prompt, output schema and tool permissions together. For example, a documentation agent can keep `Plan`, `Subtask` and `Review`, restrict edits to documentation paths, replace storefront-specific instructions, and run documentation checks instead of Node cart tests.

**3. Extract the integration code behind trusted adapters.**

| Original file | Useful definitions | Required adaptation |
| --- | --- | --- |
| [`agentic_sdlc/agents/tools.py`](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/10967999c9a9a43a97d8353b23dfea4aa7810913/agentic_sdlc/agents/tools.py) | `list_files`, `read_file`, `search_code`, `write_file`, `edit_file`, `run_tests`; checkout reconstruction and pushed-commit workspace patterns. | Remove harness decorators, `Injected`, activity settings and heartbeat wrappers. Inject the repository and assigned branch through trusted server context. Keep path/output limits. Replace local-only locks with ownership and fencing that work across runners. |
| [`agentic_sdlc/activities/github.py`](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/10967999c9a9a43a97d8353b23dfea4aa7810913/agentic_sdlc/activities/github.py) | Typed inputs/results and API bodies for branches, PRs, comments, reviews, statuses and merge operations. Existing markers and lookups are useful starting points. | Remove activity registration, Temporal error handling and demo fault injection. Add stable operation IDs, uncertain-outcome reconciliation and expected-head-SHA preconditions. A remote lookup alone does not prove an operation is safe to repeat. |
| [`agentic_sdlc/activities/checks.py`](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/10967999c9a9a43a97d8353b23dfea4aa7810913/agentic_sdlc/activities/checks.py) | `TestRun` and the `run_branch_tests()` pattern: independently test the pushed code rather than trusting an agent's `tests_passed` statement. | Run tests in a separate credential-free executor and record the exact tested commit. |
| [`agentic_sdlc/web/webhooks.py`](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/10967999c9a9a43a97d8353b23dfea4aa7810913/agentic_sdlc/web/webhooks.py) | Extract `verify()` for HMAC checks, `route()` for event classification, comment normalization, repository filtering and bot filtering. | The module imports Temporal and pipeline code. Replace `handle()` and `_start()` with a committed inbox and transactional run creation. Replace task-queue ownership labels with your application's scope. |

Keep one branch per coding lane. Reconstruct checkouts from remote commits after a restart, and treat local files as caches. For writes, use an operation marker and expected Git SHA; the source's “new text already exists” edit heuristic is insufficient to establish durable operation identity.

Do not copy `run_sandboxed()` as your security boundary. Its source explicitly describes a clean environment, temporary home and timeout as a demo-grade boundary. Generated code needs a separate executor with no GitHub, model, database or cloud credentials, bounded input/output, and filesystem/network isolation. Offer arbitrary `run_command` only if your use case needs it.

**4. Build one small agent before recreating all three scenarios.**

A useful first implementation is a PR reviewer:

1. Accept an authorized request for one allowlisted repository and PR.
2. Fetch and record the exact PR head SHA and diff.
3. Run the reviewer using `ReviewRequest`, the extracted reviewer instructions and `Review` as the output contract. Start with no write tools.
4. Save its messages, structured result and estimated usage.
5. Recheck the head before publishing comments or a status. If it changed, review the new head.
6. Publish actionable findings through your GitHub adapter using a stable operation ID.
7. Prove restart and duplicate-request behavior before adding editing tools.

For fork PRs, keep the agent suggestion-only. Add coder access and isolated tests next; add planning, parallel lanes and release behavior only when required.

A suggested project layout is:

```text
my_agent/
  models.py       # contracts copied/adapted from models.py
  prompts.py      # extracted strings; no runtime imports
  providers.py    # adapted registry construction and settings
  workspace.py    # adapted repository operations
  github.py       # adapted GitHub API operations
  runtime.py      # new model/tool stepping and checkpoint interface
  state.py        # new durable transitions and human gates
  runner.py       # new task claims, retries and reconciliation
  web.py          # new FastAPI inbox and read APIs
migrations/       # database schema and compatible upgrades
tests/            # behavioral tests and failure cases
```

**5. Supply the recovery mechanisms that the original runtime provides.**

An ordinary `Agent.run()` call can prove the agent works in one process. It does not provide the reference application's recovery behavior. If a worker dies halfway through a turn, saving only the final answer loses the intermediate model/tool progress.

Use PostgreSQL as the authoritative record of runs, pinned pipeline versions, agent histories, checkpoints, tasks, gates, comments, timers, budgets, external operations and ordered UI events.

Verify webhook signatures against the raw request body, validate the repository and request size, and commit each accepted delivery before acknowledging it. Database constraints should deduplicate delivery IDs, comment IDs and logical run creation. Give the runtime a single-step contract that returns a new checkpoint, usage and next action after one model response or tool result; commit that checkpoint before scheduling the next action.

| Original mechanism | Replacement to implement |
| --- | --- |
| Workflow state and execution history | Persisted run state and model/tool checkpoints. Commit each transition with its next tasks and events. |
| Activities and activity retries | Durable tasks and an external-operation ledger. Persist attempts, retry classification, `Retry-After` and next eligible time. |
| Child agent workflows | Independently checkpointed role sessions and parallel coding lanes. Completed lanes should remain complete after restart. |
| Signals and workflow waits | A deduplicated inbox of authorized human commands and comments. Human waits hold no database transaction or task lease. |
| Durable sleeps | Database deadlines for review windows, retry delays, canary checks and compensation. Reconcile overdue work after downtime. |
| Worker dispatch and failure detection | Short task claims with `FOR UPDATE SKIP LOCKED`, renewable leases, and fencing tokens that reject stale completions. Release locks before network calls. |
| Worker Versioning | A pipeline version pinned on each run. Keep compatible handlers available for existing versions. |
| Workflow queries/memo and Redis events | Database snapshots and ordered durable events, with reconnectable SSE offsets. |

Before a model request, reserve estimated spend transactionally across all lanes. Persist the response, message history and usage before scheduling the next model/tool action. Unknown pricing must remain unknown; the reference's `estimate_cost()` fallback returns zero and should be changed.

For a GitHub mutation, record the operation intent before calling GitHub. Checkpoint the result afterwards. On an uncertain outcome, reconcile using stable remote markers, branch names and commit SHAs. Pause when the outcome cannot safely be established. A crash between a remote success and its database checkpoint can repeat a paid model call or leave an uncertain external effect; neither implementation should promise exactly-once provider billing or arbitrary external writes.

**6. Use the pipeline files as behavior specifications.**

These files describe the application lifecycle well, but their execution code depends on Temporal. Port their decisions into durable transitions rather than replacing decorators with background coroutines.

| Original file | Behavior to retain when needed |
| --- | --- |
| [`pipeline/workflow.py`](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/10967999c9a9a43a97d8353b23dfea4aa7810913/agentic_sdlc/pipeline/workflow.py) — `IssueToProduction` | Clarify → plan approval → parallel coders and branch tests → integrate → review → merge approval → staging → release approval → canary → promote or compensate. The replacement should also test the final integrated commit. |
| [`pipeline/base.py`](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/10967999c9a9a43a97d8353b23dfea4aa7810913/agentic_sdlc/pipeline/base.py) | Human commands, conversations, feedback versus scope changes, spend checks, agent calls, PR follow-up and shared run state. |
| [`pipeline/dependency.py`](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/10967999c9a9a43a97d8353b23dfea4aa7810913/agentic_sdlc/pipeline/dependency.py) — `DependencyRepair` | Test the upgrade; diagnose failure; repair callers; explain what changed; review and respond to feedback. |
| [`pipeline/review.py`](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/10967999c9a9a43a97d8353b23dfea4aa7810913/agentic_sdlc/pipeline/review.py) — `PullRequestReview` | Review a human PR, post comments/status, address feedback and review again. |
| [`pipeline/environments.py`](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/10967999c9a9a43a97d8353b23dfea4aa7810913/agentic_sdlc/pipeline/environments.py) and [`activities/deploy.py`](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/10967999c9a9a43a97d8353b23dfea4aa7810913/agentic_sdlc/activities/deploy.py) | Optional storefront release simulation: commit pointers, traffic weighting, health checks and rollback. This does not deploy your agent application to AWS. |

Correct the reference's shortcuts as you port behavior:

- Authorize commands that approve work, increase spend, merge, release or stop execution. Recognizing a slash command is insufficient authorization.
- Bind tests, approving review, status and merge authorization to the final commit SHA. Run integrated tests and obtain approval again after fixes or conflict resolution.
- Serialize ownership of shared release environments; compensation must only undo the owning run's release. Require fresh health evidence after downtime.
- Reserve parallel spend before calls; do not rely only on a check of completed turn costs.

**7. Reuse the console and fixtures selectively.**

Start with [`web/src/components/PipelineGraph.tsx`](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/10967999c9a9a43a97d8353b23dfea4aa7810913/web/src/components/PipelineGraph.tsx), `Markdown.tsx`, `web/src/kit/StepList.tsx`, `kit/status.ts` and styles. Adapt `RunConsole.tsx`, `Panels.tsx` and `ScenarioDialog.tsx` for progress, gates, conversations and model settings. Preserve font license files with copied assets and use appropriate branding for your application.

Rewrite `web/src/api.ts` and `live.ts` around your snapshots and durable event offsets. Replace Temporal history links and workflow IDs with your own persisted history. `Acts.tsx`, `Scorecard.tsx`, `CodePanel.tsx` and `story.ts` contain demo-specific presentation claims or source mappings; remove or rewrite them. The original `web/app.py` is closely tied to Temporal queries, history, version APIs and Redis, so create a new FastAPI backing service.

If keeping the same storefront, copy the entire [`sandbox-template/`](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/tree/10967999c9a9a43a97d8353b23dfea4aa7810913/sandbox-template), including `AGENTS.md`, tests, `package.json` and the HTML import map. It depends on `orbit-money`; the prompt's “no dependencies” wording does not describe the actual fixture. Keep Node and browser dependency versions consistent. `sandbox-samples/discount-rounding/` and `agentic_sdlc/issues.yaml` provide additional scenarios. Replace these together if changing domains.

**8. Port the tests, then prove recovery.**

Use [`tests/test_pipeline.py`](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/10967999c9a9a43a97d8353b23dfea4aa7810913/tests/test_pipeline.py) for scenario fixtures, fake-world behavior and assertions. Its Temporal workers, fake workflows and signal delivery need replacement. Extract HMAC and event-routing cases from [`tests/test_webhooks.py`](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/10967999c9a9a43a97d8353b23dfea4aa7810913/tests/test_webhooks.py); do not import its Temporal-bound modules into the new service.

Before describing the new agent as durable, verify these outcomes with fake providers and a small live rehearsal:

- Duplicate/reordered webhooks create one logical request; unauthorized or stale approvals cannot advance it.
- Restart during a model/tool turn resumes saved progress; finished parallel lanes do not restart.
- Death after a GitHub success reconciles the effect or pauses without blindly repeating it.
- Expired leases allow takeover, while delayed former workers cannot commit results.
- Approval waits, retry deadlines and canary/compensation state survive downtime.
- Concurrent reservations respect the run budget, and executor failures do not expose credentials.

**9. Leave the Temporal plumbing behind and deploy the new application separately.**

Do not carry over `agentic_sdlc/worker.py`, `temporal_client.py`, `agents/harness_ext.py`, `versions.py` or `faults.py`. `agentic_sdlc/sandbox.py` configures Temporal's workflow sandbox; it is unrelated to isolating generated code. Replace the original development launcher, Compose configuration and deployment manifest rather than copying them unchanged.

For the earlier AWS/demo-registry requirement, use ordinary web and runner components plus the registry's isolated executor profile. Keep private RDS provisioning separate, supply secrets only to components that need them, and omit Temporal runtime resource requests. Validate against the registry's current schema when preparing deployment. A working local agent and the storefront's simulated release do not establish that the application is deployed in AWS.

For a concrete replacement implementation, the separate [box-agent-sdlc application](https://github.com/temporal-sa/box-agent-sdlc) contains `box_agent_sdlc/agents/runtime.py`, `integrations/workspaces.py`, `integrations/github.py`, `engine.py`, `state.py` and migrations. Its [verification record](https://github.com/temporal-sa/box-agent-sdlc/blob/main/docs/verification.md) distinguishes tested behavior from pending hosted deployment and comparison work. Use it as an implementation example alongside this map of the original source.

<a id="structured-comparison"></a>

**10. Run a structured side-by-side test of MTTD, MTTR and availability.**

Compare the Temporal application and your replacement against the same user outcomes. Treat the following as a proposed experiment protocol. The probe adapter, fault hooks and evidence collector need to exist for each application before a case can be measured; this guide does not supply an automated benchmark or measured comparison results.

**Fix the experiment before running it.** Record both source revisions, pipeline version, fixture commit, prompts, model/provider/version, output limits, budget, concurrency, CPU/memory, replica counts, restart policy, heartbeat/lease timeouts, retry rules and managed-service configuration. Use separate sandbox repositories and release environments seeded from the same commit. Match application capacity and functional safeguards; disclose differences in the backing infrastructure rather than implying that Temporal Cloud and a particular RDS configuration are identical deployments.

Run two separately reported profiles: deterministic fake model/GitHub/executor responses to isolate orchestration behavior, followed by a small live-integration profile. Keep successful model outputs and external responses equivalent in the deterministic profile. Do not pool fake and live measurements. Use the same human decisions with a fixed response delay in a sandbox harness; record real presenter delays separately.

Start with a no-fault control. Suggested engineering-screening defaults are a two-minute warmup, a fixed 15-minute scored window, and ten paired deterministic trials per fault. Alternate or randomize which implementation runs first, record the order, and avoid shared-provider quota contention. A few budgeted live trials can confirm integration behavior, but are insufficient to establish tail latency or production availability. These are proposed test settings, not established reliability targets; change them before the experiment if the workload needs longer windows.

**Measure three service surfaces separately.** Define a good probe by its correctness and deadline, using the same specification on each side. Following the [Google SRE guidance on SLIs](https://sre.google/workbook/implementing-slos/), use successful eligible events divided by all eligible events; the concrete probe rules below are choices for this experiment.

| Surface | Probe and proposed deadline | What counts as good |
| --- | --- | --- |
| Console/API | Authenticated run-state read every five seconds; two-second timeout. | Correct run identity and schema, with the persisted state available within the deadline. HTTP 200 with an invalid or wrong snapshot fails. `/readyz` alone is insufficient. |
| Durable request acceptance | Deliver a valid, signed, scoped test event every ten seconds; two-second acknowledgement deadline. | Accepted work has a verifiable durable inbox/run record and one logical identity. A quick acknowledgement without durable acceptance fails. Use controlled rehearsal events so probes do not create unintended GitHub work. |
| Execution | Every ten seconds schedule a bounded, independent task through the tested queue, runner and checkpoint path; require its correct durable result within 30 seconds. | Eligible work finishes within its deadline. An accepted request stuck in a queue fails this SLI even if the console remains available. Reuse a dedicated probe run and respect the three-active-run cap. |

For deterministic execution probes, use fake external services. Label this measurement **orchestration execution availability**. Measure an equivalent, bounded provider/executor path separately in the live profile; a fake task bypassing the model provider cannot establish live end-to-end availability. Record submission failures and capacity rejections as failed eligible probes, rather than dropping them. Retain the scheduled probe denominator, including timeouts. Score execution probes at their scheduled deadlines, in cohort order, even if results arrive out of order. State the evaluated cohort/window and allow every included deadline to expire before scoring it.

An intentional approval gate, future timer or permitted backoff is a healthy workflow state. Do not label it stalled because no new checkpoint appears. Measure due work against its declared deadline, and continue independent probes during those waits. Conversely, an overdue retry or timer is not excused by labelling it a wait. If a surface has no eligible probes, report availability as **N/A**, with its denominator, rather than 100%.

**Use an independent collector and explicit timestamps.** Run probes and collect receipts outside the worker or service being terminated. Use one collector's monotonic clock and receipt times for cross-host elapsed measurements; preserve component-generated timestamps separately. Retain UTC timestamps, clock offsets and sampling resolution for correlation. Five-second probes cannot establish subsecond impact timing. Count no-fault control alerts as false positives rather than assigning them to a later injection.

| Timestamp | Required evidence |
| --- | --- |
| `t_fault` | Confirmation that the scoped fault actually took effect, rather than the time an injection request was submitted. |
| `t_impact` | First observed breach of the chosen service objective: a failed read/acceptance probe or a missed execution deadline. Retain the last good observation to bound sampling uncertainty. |
| `t_detect` | First automatically recorded detection signal attributable to that fault, collected independently: for example a worker timeout/expired-lease event. Predeclare the signal for each case. The injector's knowledge of its own action does not count. |
| `t_alert` | First operator-visible automated alert received by the collector. Keep this separate from internal runtime detection. |
| `t_resume` | First correct new checkpoint for the interrupted work after the fault; verify the expected task, commit or operation identity. |
| `t_restore` | First evaluation in a three-good streak after that surface's observed impact, confirmed by the subsequent two evaluations. Evaluate each surface independently. For execution, only streaks beginning after the interrupted work resumes safely or completes the required compensation qualify. API recovery does not depend on that execution outcome. Process startup alone does not establish recovery. |

MTTR can refer to repair, recovery, response or resolution, so publish the exact convention being used. [AWS describes these different uses of MTTR](https://aws.amazon.com/what-is/mttr/). For this protocol:

| Metric | Per-trial calculation and aggregation |
| --- | --- |
| Runtime MTTD | Mean of `t_detect - t_fault` for trials with a valid internal detection signal. Group by fault type and detection rule. |
| Operator MTTD | Mean of `t_alert - t_fault`; report separately so a self-healing event is not mistaken for an operator notification. |
| MTTR — service restoration | Mean of `t_restore - t_impact` for impacted trials that restore the declared service objective. It includes detection delay after impact. Report each surface separately. |
| Fault-to-resume latency | `t_resume - t_fault`, showing when interrupted work makes verified progress. This can be measured even when redundancy prevents a service outage. |
| Observed availability | `100 × good eligible probes / all eligible probes`, separately for reads, durable acceptance and execution in the fixed window. Aggregate raw counts rather than averaging percentages with unequal denominators. |

If you also report detection-to-restoration time, label it explicitly; do not silently substitute it for the MTTR above. A detection alert that arrives after restoration is a late alert, not a negative repair time. A fault masked by redundancy can have **zero service downtime** and measured fault-to-resume latency; service MTTR is N/A when no service objective was violated. A safe pause for an ambiguous mutation protects correctness, but does not count as restored execution if eligible work still misses its deadline.

**Inject the same failures at equivalent semantic boundaries.** Trigger once using a fault ID tied to a run/task/operation, and record confirmation. Inject after the same model response or remote effect, rather than at an arbitrary equal wall-clock second.

| Case | Matched fault | Required recovery outcome |
| --- | --- | --- |
| Worker loss | Terminate the worker during planning or one parallel coding lane. | Resume checkpointed work; completed sibling lanes remain complete. |
| Lost mutation acknowledgement | GitHub accepts a marked comment/PR operation; terminate before its local result checkpoint. | Reconcile the existing effect or pause visibly; inspect GitHub for duplicates. |
| Provider throttling | Return 429 with the same `Retry-After` for a fixed number of requests. | Observe the deadline, bounded attempts and spend; resume after the provider recovers. |
| Executor outage | Make the assigned executor unavailable for a fixed duration during tests. | Preserve the workspace and resume isolated testing without losing the other lane. |
| Human wait | Restart while the same plan/merge/release approval is pending. | Retain the exact gate and require an authorized, current approval. |
| Canary/compensation | Fail the same health check, then interrupt compensation. | Prevent promotion and complete compensation for the owning release. |

Test backing-store/control-plane outages in a separate, clearly labelled family. A PostgreSQL outage is not automatically equivalent to a Temporal Cloud outage. Record the affected dependency and failure scope; mark a counterpart **not run** if its fault cannot be exercised. Keep imposed outage duration and fault-clear time alongside recovery latency so repair policy is distinguishable from how long the dependency was deliberately held down.

**Retain failures and publish the paired evidence.** Continue probing for the full scored window, including the fault and recovery. Record all manual interventions and their times; a rescued trial is not autonomous recovery. Do not reset state or extend an observation window for only one implementation. A missing detection at the cutoff is undetected; an unrestored service is unrecovered. Preserve these counts and lower bounds as right-censored observations. Do not enter zero or quietly omit them from the report. A mean over detected/recovered trials must be labelled conditional on those successes, with the corresponding counts alongside it.

Use this unfilled results template, with one row per implementation, fault and profile. Retain one underlying record per trial and the pairing ID.

| Implementation / fault / profile | Trials | Detected / alerted | Runtime / operator MTTD | Impacted / restored | Service MTTR | Undetected / unrecovered / inconclusive |
| --- | --- | --- | --- | --- | --- | --- |
| Temporal / … / deterministic | Not run | — | — | — | — | — |
| PostgreSQL / … / deterministic | Not run | — | — | — | — | — |

| Implementation / fault / profile | Read availability, good/total | Acceptance availability, good/total | Execution availability, good/total | Repeated calls / duplicate effects / lost progress | Manual actions / estimated spend |
| --- | --- | --- | --- | --- | --- |
| Temporal / … / deterministic | Not run | Not run | Not run | — | — |
| PostgreSQL / … / deterministic | Not run | Not run | Not run | — | — |

Keep raw probe logs, fault receipts, configured deadlines, histories/events, GitHub IDs/SHAs, operation records, actual model usage and restoration evidence. Report individual observations and paired differences, then means and medians with sample counts. Small samples do not support strong p95 claims. Probe or collector failures make a trial inconclusive unless independent evidence establishes the service result; disclose and repeat it rather than counting missing observations as successful.

These are **observed test-window availability** measurements. Deliberately chosen fault frequency and short windows do not establish monthly/annual production availability or a managed service's SLA. Record correctness violations alongside the timing metrics even when HTTP probes stay green. Use the [existing failure-comparison checklist](comparison.md) for the broader scenario and implementation-effort evidence.
