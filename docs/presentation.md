# Walkthrough for Sesh at Box

The demo keeps the same SDLC problem in view while making the durability foundation visible. One application uses the Temporal reference; this separate application stores orchestration state in PostgreSQL. The discussion should follow observed behavior and the engineering required to support it. No comparative result has been established until both implementations have run the same protocol.

## 1. Establish the boundary

Open the baseline console and identify its execution mode, workflow version, runner state and durable store. Explain that the registry deploys the application into the existing AWS environment, while RDS PostgreSQL is an external dependency. A scripted run makes control flow easy to inspect; a live run is necessary to evaluate provider behavior, GitHub effects, workspace recovery and release actions.

Introduce the three workflows: dependency repair, PR review, and an issue that proceeds through a plan, coding lanes, review and release gates. Select the same scenario and repository starting point for both implementations. Show the comparison protocol before starting the trial so the expected outcome is fixed in advance.

## 2. Follow the work

Start an issue scenario in supervised mode. Walk through the recorded plan and approve it through the authorized channel. Follow independent coding lanes, attempts, conversation and usage. When the run pauses, show the stored gate and leave it pending while interrupting a runner. The console should continue to display saved progress even when no runner is healthy.

Resume the runner and inspect the next durable checkpoint. Verify which agent outputs were reused and which operations were attempted again. Inspect actual branch and GitHub objects when live adapters are in use. A run continuing to completion establishes less than proving that it avoided repeated external effects.

## 3. Exercise recovery boundaries

Use one scoped failure at a time. Interrupt an active attempt, inject a provider rate limit, fail an executor, or trigger the supported canary fault. Record the fault point and follow its audit event to the recovery result. For the canary case, inspect the recorded health outcome and compensation; do not use a simulated SHA as evidence that traffic moved in AWS.

Show the v1/v2 control with an existing run paused at a gate. A new-run version change should leave the existing run pinned to its original implementation. Releasing an old runner version requires checking that no active runs still depend on it.

## 4. Compare equivalent evidence

Repeat the same cases in the Temporal reference. Download the baseline run record, collect the reference workflow history, and retain external-effect evidence for both. Put recovery delay, repeated work, operator intervention and integration outcomes beside each other. Label unavailable or inconclusive cases directly.

Close the walkthrough with the application work behind each outcome: persistence, timers, messages, task ownership, retry policy, checkpoint granularity, versioning, reconciliation and repair. Some integration safeguards belong in either application. Other mechanisms are provided by the chosen durability foundation or must be implemented and operated by the team.

## Discussion prompts

- Which interruption boundaries must recover automatically for the intended Box use case?
- How much repeated provider spend or operator intervention is acceptable after an uncertain result?
- Which approval waits and code rollouts must remain compatible for days or weeks?
- Who owns the database recovery, reconciliation logic and stranded-run repair procedure?
- What evidence is still missing before selecting a durability foundation?

These questions are proposed evaluation criteria. They do not attribute a position, requirement or conclusion to Sesh or Box.
