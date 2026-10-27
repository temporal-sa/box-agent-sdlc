# Verification record — October 27, 2026

The application is implemented and verified locally. AWS deployment is not complete: the configured AWS SSO sessions expired before infrastructure provisioning. No live model-provider rehearsal or matched Temporal/PostgreSQL comparison has been run.

## Completed checks

| Check | Observed result |
| --- | --- |
| Complete Python suite | 107 tests passed, zero skipped, in 11.21 seconds; real PostgreSQL and Linux executor included |
| Frontend | Two SSE parser tests, TypeScript compilation and production build passed |
| Linux containment and bundling | Nine real-container tests passed: secrets/process files hidden, metadata network blocked, read-only system, safe extraction, timeout/output limits, and self-contained bundling |
| Reference storefront fixture | All six original Node tests passed; initial bundle was 8,891 bytes |
| Browser console | Scenario creation, approval controls, SSE, parallel progress and persisted merge gate verified |
| Browser storefront | Six products rendered; adding a Tee and Mug produced two cart items and a $47.52 total |
| Browser isolation | Opaque origin; session storage denied; no subresource requests; API access blocked; `Origin: null` mutation rejected with HTTP 403 |
| Infrastructure | Terraform formatting/provider validation and current registry manifest validation passed |
| Local Compose | Database, migrations, web, v1/v2 runners and executor started; readiness and API checks returned HTTP 200 |

Screenshots: [console](screenshots/console.png), [storefront](screenshots/storefront.png).

## Process-loss rehearsal

`scripts/rehearse_recovery.py` terminated a real runner after an action returned but before its result checkpoint committed. The process exited with the injected code 75. A replacement reclaimed its expired lease, consumed no second crash fault, and reached the persisted plan gate. Total elapsed time was 1.611 seconds with a one-second test lease. The hosted default lease is 60 seconds; this is not a production recovery-time measurement.

This trial used scripted providers, local PostgreSQL and no GitHub or paid model calls. Its retained local record is `evidence/local-recovery.json`.

## Real GitHub rehearsal

`deploy/rehearse_github.py` exercised the dedicated private sandbox. Repeated writes, PR creation, comments, exact-SHA reviews, statuses and merges reused their identifiers. Fresh-client lookups independently found the recorded remote effects. [Rehearsal PR #4](https://github.com/temporal-sa/box-agent-sdlc-storefront/pull/4) merged only into its own rehearsal base branch. The initial `main` and `baseline` commits remained unchanged.

The first trial exposed a real limitation: a newly created issue was absent from an immediate GitHub collection read, and direct repetition created a duplicate. Both aborted issues were closed and their failure evidence retained. The adapter now reuses known responses and performs fresh reads; after a lost response, an inconclusive lookup pauses durable execution rather than treating absence as proof that creation is safe. The corrected trial passed.

This was an adapter integration trial, with no model or AWS calls. Local records are retained in `evidence/local-github.json` and the per-trial audit files. They are ignored by Git so future evidence containing private run content is not published automatically.

## Remaining deployment work

Refresh the configured AWS SSO session, discover the current private network inputs, provision the dedicated RDS stack, bootstrap narrow database/runtime/GitHub secrets from a private-network host, configure the sandbox webhook, and merge the prepared registry change through its normal review path. Verify hosted candidate readiness and perform a small live-provider rehearsal before presenting any hosted or comparative result as measured.
