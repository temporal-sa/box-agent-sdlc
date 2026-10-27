# Deploy the PostgreSQL SDLC application

This application runs as a separate project at the expected URL
`https://box-agent-sdlc.tmprl-demo.cloud`. The registry owns container builds,
EKS workloads, HTTPS, catalog authentication, candidate checks, and route
promotion. A separate infrastructure stack owns private RDS PostgreSQL and
retains its data when an application revision or registry project is removed.

The application has no Temporal runtime dependency. The shared registry uses
Temporal internally to manage deployments. The existing Temporal SDLC demo and
its sandbox remain independent.

## Run locally

```sh
python3 deploy/local-env.py
docker compose up --build -d
```

Open `http://localhost:8000`. Compose starts PostgreSQL 17, runs migrations,
serves the React console, runs one v1 runner and one v2 runner, and starts the
isolated shell executor. The console and database bind only to localhost.
Generated `.env` credentials have mode `0600` and are never printed. Local
defaults use `DEMO_MODE=scripted` and development authentication. Scripted
results must remain marked as such in the console and any comparison evidence.

The executor requires a Linux kernel with working unprivileged user namespaces.
Its `/readyz` check verifies Bubblewrap containment; the runner waits while that
check fails. Never disable the sandbox to make generated code run. If Docker
Desktop or the host policy blocks containment, use a compatible Linux host.

`docker compose down` retains the local database volume. `docker compose down
--volumes` permanently deletes that local data. Stop runners with `docker
compose stop runner-v1 runner-v2`; restart them with `docker compose start
runner-v1 runner-v2`. This is also a way to demonstrate recovery from persisted
task leases without altering the application database.

## Provision the private database

Use Terraform or OpenTofu 1.10 or later and an AWS identity authorized for this
independent stack. The default region is `us-west-1`. The stack never modifies
the registry's EKS clusters, VPC, routes, or other demos.

1. Obtain the existing platform `vpc_id` and `private_subnet_ids` from its
   nonsecret infrastructure outputs. Obtain each active EKS cluster's
   `resourcesVpcConfig.clusterSecurityGroupId` from `aws eks describe-cluster`.
   Supply the groups used by the actual demo pods. Include green when present.
   The EKS control plane also lists public subnets; choose only the platform
   subnets tagged `kubernetes.io/role/internal-elb=1` for RDS. Public-IP
   assignment alone cannot distinguish them because both platform subnet
   types disable it. The stack checks the private-subnet tag and AZ spread.
2. Copy `infra/terraform.tfvars.example` to `infra/terraform.tfvars`, replacing
   the account/network placeholders. Copy `infra/backend.hcl.example` to
   `infra/backend.hcl`, specifying an existing private, versioned state bucket.
   Use a separate state key from the registry platform.
3. Set `AWS_PROFILE` to the intended authenticated identity. Check
   `aws sts get-caller-identity`; the provider also rejects an unexpected
   `aws_account_id`.
4. Review and apply this stack:

```sh
cd infra
tofu init -backend-config=backend.hcl
tofu validate
tofu plan -out=terraform.tfplan
tofu apply terraform.tfplan
```

Terraform's equivalent commands work with the same files. The initial plan
resolves an available PostgreSQL 17 minor in the target region; record that
version in `postgres_engine_version` before a later production rollout. The
default is a `db.t4g.small` Multi-AZ instance, 20 GiB encrypted storage with
growth up to 100 GiB, TLS-required connections, and seven days of backups.
The database accepts TCP 5432 only from the supplied EKS and optional private
management security groups. No public endpoint or public CIDR rule is created.

RDS manages the administrative password in Secrets Manager. Terraform creates
empty project `runtime` and `github` secret containers; values are populated
outside Terraform and never enter its state. Database deletion protection,
`prevent_destroy`, retained automated backups, and a required final snapshot
provide separate barriers against accidental data loss.

## Populate runtime secrets

Run database bootstrap from an approved management host or VPN with private
network access to RDS. Its security group must be allowed in the infrastructure
inputs. Do not make the database public to bootstrap it. The host needs the
AWS CLI and the application's Python dependencies (`uv sync`). Download the
[official RDS CA bundle](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.SSL.html)
to that host, then run:

```sh
uv run python deploy/bootstrap-secrets.py \
  --profile YOUR_PROFILE \
  --ca-bundle /path/to/global-bundle.pem \
  --github-command-actors authorized-login,another-authorized-login \
  --set-github-token
```

The helper privately prompts for the GitHub token, generates database and
runtime credentials in memory, creates a restricted `box_app` database login,
and writes the project JSON secrets directly to AWS. It never prints secret
values, writes them locally, or includes them in process arguments. Only
bootstrap operators need access to the RDS-managed administrative credential;
the application receives the restricted login.

The `runtime` secret lives at `tmprl-dem-cld/box-agent-sdlc/runtime` and contains
`DATABASE_URL`, `GITHUB_WEBHOOK_SECRET`, `EXECUTOR_TOKEN`, and
`GITHUB_COMMAND_ACTORS`. The database URL uses certificate and hostname
verification; the app image includes the RDS CA bundle at
`/app/certs/rds-global-bundle.pem`. The `github` secret contains `GITHUB_TOKEN`.
Existing database credentials and runtime tokens are preserved on rerun.
An interrupted first bootstrap after role creation can be recovered with
`--rotate-database-password`; intentional rotation requires a maintenance
window and a subsequent app rollout so all replicas receive the new value.

The separate private sandbox `temporal-sa/box-agent-sdlc-storefront` has been
seeded from the pinned reference's `sandbox-template`. Its initial `main` and
`baseline` branches point to `69d7528a8ec467b73bc3505b4676f41e5bfd10a2`, and
their Git tree exactly matches the reference fixture. The full mapping is in
`deploy/sandbox-provenance.json`. Its main branch requires pull requests and
rejects force pushes and deletion. The `agent`, ownership
`agentic-sdlc:box-agent-sdlc`, and `dependencies` labels are installed.
Restrict its fine-grained GitHub token
to that repository with only the contents, pull-request, issue, commit-status,
and Actions permissions actually needed by the enabled scenarios. Keep the
original `temporal-sa/agentic-sdlc-storefront` outside its scope. Preserve
baseline/main protections and review any branch-protection changes separately.

The manifest reuses the registry's existing shared `anthropic`, `openai`, and
`gemini` provider secrets. It maps `GEMINI_API_KEY` to application
`GOOGLE_API_KEY`. Those keys go only to runners. If shared keys are unavailable,
use project-owned JSON secrets and remove `shared` from their declarations.
Never put values in a manifest, shell history, a source file, a plan, or a
comparison report.

## Deploy through the registry

Push the source to `temporal-sa/box-agent-sdlc` on `main` after its tests and
container builds pass. Add this repository's `deploy/demoproject.yaml` to the
registry as `projects/demo/box-agent-sdlc.yaml`. That one CR is the only
application deployment file committed to the registry. Do not copy Compose,
Terraform, Kubernetes objects, Helm charts, image tags, or credentials there.

Validate the entire registry before its PR is merged:

```sh
python3 scripts/validate_projects.py
```

Flux delivers the CR. The operator builds immutable app and executor images,
waits for candidate availability, checks `GET /readyz`, and promotes its HTTPS
route on success. Check `DemoProject.status` and catalog rollout status before
announcing a live URL. The expected components are `web`, `runner-v1`,
`runner-v2`, and `executor`. Each app process runs the migration CLI before
startup; a database advisory lock serializes additive upgrades when candidate
and active revisions overlap.

The manifest uses ordinary components without `worker: true`,
`temporalAccess`, `spec.temporal`, or Temporal input references. Registry
`worker` flags mean Temporal workers and must stay absent. Keep the registry's
`crashable` and Temporal autoscaling features disabled for this application.
Its application fault controls and operator restart procedure demonstrate
PostgreSQL recovery.

The console is protected by registry SSO with `AUTH_MODE=proxy`. The public
`/webhooks/github` route bypasses SSO only so GitHub can deliver signed events;
the application verifies its signature. Never expose a proxy-authenticated
container directly to untrusted traffic. The executor has no public route,
database connection, model key, GitHub token, AWS role, or service-account
token. The registry's `isolatedExecutor` profile provides the outer firewall
and read-only filesystem; Bubblewrap provides a fresh sandbox for each command.

After promotion, configure the dedicated sandbox webhook:

```sh
uv run python deploy/configure-webhook.py --profile YOUR_PROFILE
```

This requires a GitHub administrator identity in `gh` and permission to read
only the project runtime secret. The helper sends the signing secret over
standard input and suppresses provider responses. It creates or updates the
matching URL without changing other webhooks. Enable issues, issue comments,
pull requests, pull-request reviews, and pull-request review comments. Inspect
GitHub's recent-delivery status and exercise one labeled issue before starting
a customer demonstration.

## Verify recovery and operate releases

For a GitHub-only adapter check, run `uv run python deploy/rehearse_github.py`
with an authorized `gh` identity. It captures the CLI credential only in
process memory, creates marked issue/PR evidence in the private sandbox, and
merges into its own `rehearsal/.../base` branch. It verifies repeated writes,
comments, reviews, statuses and merges, plus fresh-client remote lookups.
`main` and `baseline` must remain unchanged. Sanitized evidence is saved under
the ignored `evidence/` directory; this check makes no model or AWS calls and
does not establish a comparative durability result.

First verify `/readyz`, runner heartbeats, executor `/readyz` from the private
network, and a small live run. A signed webhook must be acknowledged only after
its durable inbox entry commits. Check that the run's mode is `live` and that
the linked GitHub evidence belongs to the separate sandbox.

For a fair comparison, use matching scenario inputs, model/provider settings,
budgets, and approval decisions. Record retries, repeated model calls, uncertain
external effects, recovery time, and the configured task-lease duration. A
terminated runner becomes recoverable after its lease expires; immediate
resumption is not guaranteed. Durable checkpoints do not make arbitrary shell,
model-billing, or GitHub effects exactly once.

Use the app's audited fault controls for normal demonstrations. A platform
operator may terminate only this project's runner pod to test replacement.
Versioned run state stays pinned to v1 or v2; keep compatible runners available
until their old runs drain. Never update database state by hand to manufacture
a recovery result.

Source changes create another registry candidate. Failed candidates leave the
existing route active; revert the source commit to roll back application code.
Database changes must remain additive across overlapping revisions. A code
rollback does not undo a migration, GitHub mutation, or completed deployment.
Investigate exhausted tasks and uncertain external outcomes in the console
before choosing an explicit retry or reconciliation action.

Removing the registry CR tears down application runtime resources. RDS and the
project secret containers live in this independent stack and are retained.
Permanent database deletion requires an intentional infrastructure change to
both protection settings and a unique final snapshot name. Review and retain
that snapshot and backups before deleting data or retiring credentials.
