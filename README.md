# Box Agentic SDLC — PostgreSQL baseline

A separate implementation of the Agentic SDLC reference for a technical evaluation with Sesh at Box. FastAPI and React expose the same three stories: issue delivery with parallel coders, dependency repair, and pull-request review. PostgreSQL stores task leases, model/tool checkpoints, human gates, timers, budgets, operation results, and release ownership. The application contains no Temporal runtime dependency. The shared demo registry continues to use its existing control plane.

This is an application-specific durable state machine. It is not a general workflow platform. Execution attempts are at least once. External writes need reconciliation; a model response that was never checkpointed can be billed again. The interface distinguishes deterministic scripted runs from live GitHub/model work.

## Run locally

Install Docker, Python 3.13 with uv, and Node 22+. Generate local runtime configuration, then start the containers:

```sh
python3 deploy/local-env.py
docker compose up --build
```

Open the console at http://localhost:8000. Local Compose uses labelled scripted agents and no paid providers. The executor requires Linux user namespaces and Bubblewrap; its readiness fails closed if containment is unavailable.

For faster development with an existing PostgreSQL server:

```sh
uv sync
uv run python -m box_agent_sdlc.migrate
AUTH_MODE=development DEMO_MODE=scripted uv run uvicorn box_agent_sdlc.web:app --port 8000
AUTH_MODE=development DEMO_MODE=scripted PIPELINE_VERSION=v1 uv run python -m box_agent_sdlc.runner
AUTH_MODE=development DEMO_MODE=scripted PIPELINE_VERSION=v2 uv run python -m box_agent_sdlc.runner
npm --prefix web ci
npm --prefix web run dev
```

The default development database is `postgresql://box:box@localhost:5432/box_sdlc`. Use these credentials only for the loopback-bound disposable development server. Vite forwards API requests to port 8000. Build the console with `npm --prefix web run build` to serve it directly from FastAPI.

## Live configuration

Use `DEMO_MODE=live`. Runners require `GITHUB_TOKEN`, `EXECUTOR_URL`, `EXECUTOR_TOKEN`, a supported provider key, and a narrow GitHub sandbox repository. The web process does not receive the GitHub write token or provider keys. Configure repository webhooks with `GITHUB_WEBHOOK_SECRET`; only committed, signature-verified deliveries are acknowledged.

For local Compose, set `DEMO_MODE=live` and `AUTH_MODE=token` in the private `.env` file alongside the required GitHub/provider credentials, then recreate the application services with `docker compose up -d --build web runner-v1 runner-v2`. Open **Operator access** (the gear icon) in the console and apply the existing `OPERATOR_TOKEN` from `.env` for this browser session. Development authentication is allowed only in scripted mode. The executor receives no GitHub or provider credentials in either mode.

Use a dedicated sandbox seeded from the pinned reference fixture. Label externally opened issues/PRs `agentic-sdlc:<APP_SCOPE>`; issues also require `agent`. Owner labels keep this application and the Temporal reference from responding to the same request. The application never merges into the shared repository's main branch; issue delivery merges only into its own run base branch.

Role model choices accept `{provider, model}` for `planner`, `coder`, and `reviewer`. Providers are Anthropic, OpenAI, and Google. Pin identical choices in matched comparisons. Defaults are compatibility defaults, not a claim that they match the reference's configured models; record explicit overrides before comparison. Known model pricing is required before paid requests; request limits, output caps and transactional reservations limit spend.

Set `DEFAULT_PLANNER_PROVIDER`, `DEFAULT_CODER_PROVIDER`, and `DEFAULT_REVIEWER_PROVIDER` to choose the provider for each role. `DEFAULT_OPENAI_MODEL`, `DEFAULT_ANTHROPIC_MODEL`, and `DEFAULT_GOOGLE_MODEL` select their model IDs. For an OpenAI-only local setup, set all three role providers to `openai`; the existing OpenAI model default is `gpt-4.1`. New runs save the resolved choices, and the console shows them before a run starts.

GitHub commands `/approve`, `/merge`, `/ship`, `/continue`, `/skip`, and `/stop` are accepted only from allowed actors or repository collaborators with write permission. Ordinary comments can revise plans, request fixes, change scope, or steer coder turns. Approval, tests and review are bound to a commit SHA.

## Durability and recovery

- The task table is also the durable scheduler. Transitions, subsequent tasks and audit events commit in one database transaction.
- Runners claim short-lived leases using `SKIP LOCKED`. Heartbeats renew them; a new fencing token rejects delayed completions after takeover. No lock spans a model/GitHub/executor call.
- Each model request and tool invocation is a separate durable agent step. Completed operation results are reused. Uncertain mutations reconcile against stable remote markers or pause for intervention.
- Human gates and retry/canary deadlines are persisted timestamps. New runner identities resume the same work; waiting does not occupy an execution slot.
- Git commits and durable patches reconstruct ephemeral workspaces. Generated code runs in a credential-free network-isolated executor, not in the runner.
- Pipeline version is pinned on creation. A v1 runner cannot claim v2 tasks. Candidate and active registry revisions can coexist against the database.
- Database loss stops progress; this application cannot recover data absent from its database and backups. RDS availability and backup/restore are separate operational responsibilities.

The storefront release is a demonstration: immutable commit pointers, staging checks, sticky simulated 20% traffic, and promotion/compensation. These are separate from deploying this application's containers into AWS. Current health checks prove asset availability; they do not establish full browser behavior.

Before release, the executor bundles modules, styles and local JSON into a self-contained HTML snapshot. The web process serves that immutable snapshot from PostgreSQL with an opaque-origin CSP sandbox and no network access. Generated storefront code cannot inherit console storage or API authority, and rendering does not depend on third-party cookies or public repository access.

If remote reconciliation is inconclusive, the task pauses instead of repeating a mutation. An operator can inspect it with `uv run python scripts/resolve_operation.py --run-id <id> --list` using the configured database/scope. After verifying the actual remote outcome, use the same tool with its operation ID, request hash, a JSON result file, evidence reason and actor. This repair is audited. If the outcome cannot be established, stop the run; do not manufacture a successful result.

## Verification

Run against an expendable PostgreSQL database with the schema migrated:

```sh
uv run pytest
npm --prefix web test
npm --prefix web run build
uv run ruff check --select F,I box_agent_sdlc tests
```

Database tests use unique application scopes. Set `TEST_DATABASE_URL` for CI. Tests cover duplicate admission, task competition, expired leases, stale completions, completed-operation reuse, uncertain outcomes, budgets, gate persistence, all three scripted stories, canary rollback and version pinning. Provider/GitHub tests use fakes or local bare Git repositories. Scripted test results are not live-model or comparative findings.

## Deployment and evaluation

- [Deployment runbook](docs/deployment.md): independent private RDS, secret bootstrap, registry manifest, rollback and retained data.
- [Architecture](docs/architecture.md): boundaries, data flow and failure semantics.
- [Comparison protocol](docs/comparison.md): matched inputs, failure points and unfilled evidence ledger.
- [Presenter walkthrough](docs/presentation.md): the issue-delivery story and explicit evidence limits.
- [Verification record](docs/verification.md): completed local/browser/GitHub checks and remaining deployment work.
- [Reference provenance](docs/provenance.json): pinned source revisions.

The registry builds the Dockerfiles, publishes immutable ECR images, and rolls out the `DemoProject`. App CI tests code; it does not operate a competing image/deployment pipeline. Deployment is complete only after RDS, secrets, source registration, candidate readiness and hosted rehearsal have been verified.
