# Verification record — October 27, 2026

The application is implemented and verified locally. A live OpenAI/GitHub rehearsal reached human plan approval. AWS deployment is not complete: the configured AWS SSO sessions expired before infrastructure provisioning. No matched Temporal/PostgreSQL comparison has been run.

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

Refresh the configured AWS SSO session, discover the current private network inputs, provision the dedicated RDS stack, bootstrap narrow database/runtime/GitHub secrets from a private-network host, configure the sandbox webhook, and merge the prepared registry change through its normal review path. Verify hosted candidate readiness and repeat a small live-provider rehearsal in AWS before presenting any hosted or comparative result as measured.

## Local live-mode verification

The local console and both runners use authenticated live mode in the separate `box-agent-sdlc-live` scope. Existing scripted records remain in their original scope. Planner, coder and reviewer defaults are explicitly `openai:gpt-4.1`; the provider reported snapshot `gpt-4.1-2025-04-14`. Provider and GitHub credentials are supplied only to runners. Local credentials remain in the ignored, mode-0600 `.env` file and outside Docker build contexts.

A tiny Responses API connection check succeeded with 18 tokens. The first application trial, [sandbox issue #5](https://github.com/temporal-sa/box-agent-sdlc-storefront/issues/5), exhausted the clarifier's existing 15-request limit before returning a result. Its estimated spend was $0.104410. Its failed run and local evidence are retained; it is not counted as a successful scenario.

The runtime now offers only the structured final-result tool on the last permitted model request and asks for verified findings and remaining uncertainties. Request limits remain unchanged, and invalid final results do not earn another paid attempt. The final tool description also refers to the current role's task rather than the whole delivery pipeline. These harness changes must be disclosed in a matched comparison.

The corrected, smaller trial, [sandbox issue #6](https://github.com/temporal-sa/box-agent-sdlc-storefront/issues/6), requested a single storefront heading change. Real GitHub work and saved model/tool checkpoints led to a persisted plan-approval gate: 23 model requests, 49,270 input tokens, 595 output tokens, and $0.103300 estimated model spend. It remains supervised with a $3 run budget; no plan approval, coding, merge or release was performed. Raw local records are `evidence/live-initial-exploration-limit.json` and `evidence/live-corrected-rehearsal.json`. These estimates are application records, not a reconciled provider invoice or comparative performance result.

For these changes, 50 focused authentication, pipeline, model-selection and agent tests passed; the frontend tests/build and rebuilt application image passed. [Live console screenshot](screenshots/live-console.png).
