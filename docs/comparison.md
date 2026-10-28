# Fair durability comparison

Use this protocol to compare the PostgreSQL baseline with the Temporal reference. The purpose is to establish which failure outcomes each implementation handles and what implementation and operating work is required. It does not assume that either design wins, and it does not treat a successful scripted run as evidence of real integration behavior.

For a repeatable experiment with explicit MTTD, MTTR and availability definitions, probe rules, paired trials and results tables, use [the structured side-by-side measurement procedure](build-your-own-agent-without-temporal.md#structured-comparison). It distinguishes console reads, durable acceptance and execution availability, and keeps failed or unobserved recoveries visible.

## Fix the inputs

Record both application commits, the reference revision (`10967999` initially), registry revision (`d56184f` initially), workflow version, execution mode, repository and initial commit, model/provider/version, prompts, autonomy policy, budget, concurrency, adapter versions and fault point. Use separate copies of the same repository state so one implementation’s edits do not become the other’s starting point.

Use equivalent functional behavior in both implementations: dependency repair, PR review, and issue-to-release with the same approval policy, parallel coding shape, review policy, staging checks, 20% canary target, 90-second observation interval and compensation decisions where those behaviors are enabled. Record any missing integration as a limitation. Do not substitute a scripted outcome for a missing live adapter.

First run a no-fault control. Then run each fault case in both implementations. Repeat live model cases enough to distinguish orchestration behavior from model output variation; retain the exact number of trials and every failure rather than reporting only a successful attempt. If a fault hook is unavailable in the reference, use a controlled process interruption or provider proxy and record the difference.

## Failure cases and observations

| Case | Inject at | Inspect |
|---|---|---|
| Worker interruption | During an active task, before its result commits | Recovery delay, reused checkpoints, attempt count, repeated model/tool calls |
| Ambiguous remote effect | After GitHub or a deployment target accepts an operation, before local acknowledgement | Duplicate comments/PRs/merges, reconciliation result, unresolved uncertainty |
| Provider 429 | A specified model request | Backoff, maximum attempts, `Retry-After`, final outcome and added cost |
| Executor failure | While a coding lane is running | Workspace isolation, saved artifacts, retry scope, child-lane independence |
| Approval wait | Restart while a plan, merge or production gate is pending | Gate retention, duplicate commands, authorization, correct next step |
| Canary failure | During the observation interval | Durable deadline, target state, health evidence, promotion prevention and compensation |
| Stale worker | Let a lease expire, resume the original worker after reassignment | Rejected stale result, external effect conflict handling |
| Store/notification outage | Interrupt PostgreSQL or optional notifications | Failed acknowledgements, backlog recovery, missing events, duplicate work |
| Version rollout | Start v1, deploy v2, then resume v1’s gate | Old-run compatibility, new-run routing, drain/rollback procedure |
| Budget exhaustion | Exhaust the budget across retry attempts | Durable usage, unknown charges, blocked further spend, visible outcome |

The console’s four fault controls cover its supported application hooks. They are not evidence that every row above has an automated harness. Document manual setup and unsupported cases explicitly.

## Record the result

For every trial, retain the input, fault identity and timestamp, last committed checkpoint, recovery checkpoint, attempts, usage/cost source, audit records, relevant GitHub object IDs/SHAs and observed release state. Keep wall-clock recovery delay separate from active execution time. Export the baseline’s selected run from the Evaluation tab and obtain the equivalent workflow history and external-effect evidence for the reference.

Use these outcome labels consistently:

- **Pass:** the predefined outcome was observed and its artifacts were checked.
- **Fail:** the predefined outcome was violated; keep the complete failing trace.
- **Inconclusive:** the fault did not occur at the intended boundary or the evidence cannot establish the outcome.
- **Not run:** the case has no trial evidence.

| Case | Mode | Baseline evidence | Temporal evidence | Outcome | Notes |
|---|---|---|---|---|---|
| No-fault control | — | Not run | Not run | Not run | |
| Worker interruption | — | Not run | Not run | Not run | |
| Ambiguous remote effect | — | Not run | Not run | Not run | |
| Approval and version rollout | — | Not run | Not run | Not run | |
| Canary compensation | — | Not run | Not run | Not run | |

This table is an unfilled evidence ledger, not a scorecard of implementation capability. Update it only from retained trial records.

## Compare the work as well as the result

Count application code devoted to task claims, leases, fencing, retry classification, checkpoints, timers, approval delivery, external-effect reconciliation, version compatibility, observability and repair tools. Count infrastructure and operator dependencies separately. Include the code shared by both implementations, such as GitHub idempotency checks and workspace isolation, so it is not attributed to only one orchestrator.

Report ongoing operating tasks: database backups and restore testing, stranded-run detection, queue lag, stale worker investigation, version draining, access control, schema migrations and incident repair. Distinguish an implemented mechanism from a mechanism that has passed a failure trial. A narrower deployment with fewer dependencies may still transfer substantial reliability work into the application.

Neither checkpointing system can by itself prove exactly-once effects in an arbitrary external service. In a fair comparison, inspect how each application handles uncertainty between a remote action and its local acknowledgement. PostgreSQL’s [row-locking documentation](https://www.postgresql.org/docs/current/sql-select.html) and AWS’s [outbox guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html) describe mechanisms and remaining duplication concerns.
