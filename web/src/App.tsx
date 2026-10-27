import { useEffect, useRef, useState, type FormEvent } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, human, money, shortSha, timestamp, type Activity, type Config, type Gate, type Run, type Step } from "./api";
import { useOverview, useRun } from "./live";

const KIND: Record<string, string> = { issue: "Issue to release", dependency: "Dependency repair", review: "PR review" };
const TERMINAL = new Set(["completed", "succeeded", "failed", "cancelled", "stopped", "rolled_back"]);
const FAULTS = [
  ["worker_exit", "Worker interruption"], ["provider_429", "Provider rate limit"],
  ["executor_failure", "Executor failure"], ["canary_failure", "Canary failure"],
] as const;

function Badge({ value }: { value: string }) {
  return <span className="badge" data-status={value}><span className="status-dot" />{human(value)}</span>;
}

function MarkdownText({ value }: { value?: string }) {
  return <div className="markdown"><Markdown remarkPlugins={[remarkGfm]}>{value ?? ""}</Markdown></div>;
}

function eventText(item: Activity): string {
  const text = item.text ?? item.body ?? item.message ?? item.detail ?? item.outcome;
  if (text) return text;
  if (item.key) return `${String(item.key)}${item.error ? `\n\n${String(item.error)}` : ""}${item.fence !== undefined ? `\n\nFencing token: ${String(item.fence)}` : ""}`;
  const data = item.data;
  if (data && typeof data === "object") {
    const obj = data as Record<string, unknown>;
    const display = obj.message ?? obj.detail ?? obj.text ?? obj.error;
    if (typeof display === "string") return display;
    return JSON.stringify(obj, null, 2);
  }
  return "";
}

function ActivityList({ items, empty }: { items: Activity[]; empty: string }) {
  if (!items.length) return <div className="empty-panel"><span className="empty-symbol">· · ·</span><p>{empty}</p></div>;
  return <ol className="activity-list">{items.map((item, index) => <li key={`${item.id ?? index}-${item.type ?? "event"}`}>
    <div className="activity-heading"><strong>{item.author ?? item.role ?? item.type ?? String(item.kind ?? "Event")}</strong>
      {item.status && <Badge value={item.status} />}<time>{timestamp(item.at ?? item.timestamp)}</time></div>
    {item.model && <span className="mono muted">{item.model}{item.lane ? ` · ${item.lane}` : ""}</span>}
    {item.attempt !== undefined && <span className="mono muted">Attempt {item.attempt}{item.cost_usd !== undefined ? ` · ${money(item.cost_usd)}` : ""}</span>}
    <MarkdownText value={eventText(item)} />
  </li>)}</ol>;
}

function Pipeline({ steps }: { steps: Step[] }) {
  steps = steps.map((step) => ({ ...step, lane: step.lane ?? (step.key.startsWith("code:") ? step.key.slice(5) : null) }));
  const lanes = Array.from(new Set(steps.map((step) => step.lane).filter((lane): lane is string => !!lane)));
  const shared = steps.filter((step) => !step.lane);
  const renderStep = (step: Step) => <li key={step.key} data-status={step.status}>
    <span className="step-node">{["completed", "succeeded"].includes(step.status) ? "✓" : step.status === "failed" ? "!" : ""}</span>
    <div><strong>{step.label}</strong><span>{human(step.status)}{(step.attempt ?? 0) > 0 ? ` · attempt ${step.attempt}` : ""}</span>
      {step.detail && <p>{step.detail}</p>}</div>
  </li>;
  return <section className="card pipeline-card"><div className="section-heading"><h2>Pipeline progress</h2><span className="muted">Committed state</span></div>
    {!steps.length ? <p className="muted">The first pipeline stage will appear when a runner claims this run.</p> : <>
      <ol className="pipeline">{shared.map(renderStep)}</ol>
      {lanes.length > 0 && <div className="parallel-lanes">{lanes.map((lane) => <div className="lane" key={lane}><span className="eyebrow">{lane}</span><ol className="pipeline lane-pipeline">{steps.filter((step) => step.lane === lane).map(renderStep)}</ol></div>)}</div>}
    </>}
  </section>;
}

function AgentActivity({ run }: { run: Run }) {
  const summaries = [["plan", "Saved plan"], ["diagnosis", "Dependency diagnosis"], ["review", "Code review"], ["security_review", "Security review"]]
    .filter(([key]) => !!run[key]);
  return <div className="agent-activity">
    {!!run.agents?.length && <div className="agent-grid">{run.agents.map((agent, index) => <div className="agent-card" key={String(agent.run_id ?? index)}><span className="eyebrow">{agent.lane ? `Lane ${agent.lane}` : "Pipeline agent"}</span><h3>{human(agent.role)}</h3>{agent.model && <span className="mono muted">{agent.model}</span>}<span className="mono agent-id">{String(agent.run_id ?? agent.workflow_id ?? "")}</span></div>)}</div>}
    {summaries.map(([key, label]) => <details className="saved-output" key={key} open={key === "plan"}><summary>{label}</summary><pre className="record">{typeof run[key] === "string" ? String(run[key]) : JSON.stringify(run[key], null, 2)}</pre></details>)}
    {!run.agents?.length && !summaries.length && <ActivityList items={[]} empty="Agent activity will appear as work is recorded." />}
    {!!run.agents?.length && !summaries.length && <p className="agent-note">Agent references are committed. Saved outputs appear here when each agent completes.</p>}
  </div>;
}

function attemptsFor(run: Run): Activity[] {
  if (run.attempts?.length) return run.attempts;
  return (run.events ?? []).filter((event) => /task_claimed|task_failed|task_retry|lease|retry_exhausted/i.test(String(event.kind ?? event.type ?? "")));
}

function GatePanel({ gate, busy, onCommand }: { gate: Gate; busy: boolean; onCommand: (command: string) => void }) {
  return <section className="gate-card"><div className="gate-icon">↳</div><div className="gate-copy"><span className="eyebrow">Waiting for a decision</span>
    <h2>{gate.question ?? `${human(gate.key)} approval`}</h2><MarkdownText value={gate.context} />
    {!!gate.questions?.length && <ul>{gate.questions.map((question) => <li key={question}>{question}</li>)}</ul>}
    {gate.closes_at && <p className="muted">Deadline {timestamp(gate.closes_at)}</p>}
    <div className="button-row">{(gate.commands ?? []).map((command) => <button key={command} className={/reject|cancel|stop/i.test(command) ? "button secondary" : "button primary"} disabled={busy} onClick={() => onCommand(command)}>{human(command.replace(/^\//, ""))}</button>)}</div>
  </div><p className="gate-note">This gate is saved in PostgreSQL. It remains pending across worker restarts.</p></section>;
}

function ReleasePanel({ run, environment }: { run: Run; environment?: Record<string, unknown> }) {
  const release = run.release ?? {};
  const value = (key: string) => typeof release[key] === "string" ? release[key] as string : undefined;
  return <div className="release-panel"><div className="release-grid">
    <div><span className="eyebrow">Working branch</span><strong className="mono">{run.branch ?? run.branch_name ?? String(run.head_branch ?? "—")}</strong></div>
    <div><span className="eyebrow">Staging</span><strong className="mono">{shortSha(run.staging_sha ?? value("staging_sha"))}</strong></div>
    <div><span className="eyebrow">Canary</span><strong className="mono">{shortSha(run.canary_sha ?? value("canary_sha"))}</strong></div>
    <div><span className="eyebrow">Stable</span><strong className="mono">{shortSha(run.stable_sha ?? value("stable_sha"))}</strong></div>
  </div><p className="muted">The demo serves repository commit pointers with simulated canary routing. These records do not represent a rollout of the SDLC application; the demo registry manages that deployment.</p>
    {!!Object.keys(release).length && <pre className="record">{JSON.stringify(release, null, 2)}</pre>}
    {!!environment && !!Object.keys(environment).length && <div className="shared-environment"><span className="eyebrow">Current shared demo environment</span><pre className="record">{JSON.stringify(environment, null, 2)}</pre></div>}
    <div className="button-row">{run.issue_url && <a className="button secondary" href={run.issue_url} target="_blank" rel="noreferrer">Open issue ↗</a>}{run.pr_url && <a className="button secondary" href={run.pr_url} target="_blank" rel="noreferrer">Open pull request ↗</a>}</div>
  </div>;
}

function EvidencePanel({ run }: { run: Run | null }) {
  function download() {
    if (!run) return;
    const blob = new Blob([JSON.stringify({ exported_at: new Date().toISOString(), implementation: "postgresql-baseline", run }, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a"); link.href = url; link.download = `${run.run_id}.json`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return <section className="card evidence-panel"><span className="eyebrow">Evaluation protocol</span><h2>Observe recovery. Record the result.</h2>
    <p>Use the same scenario, repository revision, model, budget and fault point in each implementation. Compare the resulting artifacts and audit records before drawing a conclusion.</p>
    <div className="evidence-grid">
      <div><span className="number">01</span><h3>Start from the same input</h3><p>Keep workflow behavior and approval decisions consistent. Label scripted and live executions separately.</p></div>
      <div><span className="number">02</span><h3>Interrupt at a known point</h3><p>Capture the last committed step, fault event, worker attempt and next successful checkpoint.</p></div>
      <div><span className="number">03</span><h3>Inspect remote effects</h3><p>Check branches, pull requests, deployment state and provider usage for repeated or missing work.</p></div>
    </div>
    <div className="notice neutral"><strong>No comparison verdict is implied.</strong> This console shows this baseline’s records. Scripted activity proves control flow; it does not establish real provider spend, executor isolation, or deployment behavior.</div>
    <button className="button secondary" disabled={!run} onClick={download}>Download selected run record ↓</button>
  </section>;
}

function ScenarioDialog({ config, busy, onClose, onStart }: { config: Config | null; busy: boolean; onClose: () => void; onStart: (body: Record<string, unknown>, idempotencyKey: string) => Promise<void> }) {
  const [idempotencyKey] = useState(() => crypto.randomUUID());
  const [kind, setKind] = useState("issue");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [repo, setRepo] = useState(config?.repo ?? config?.github_repo ?? "");
  const [budget, setBudget] = useState(String(config?.budget_usd ?? config?.defaults?.budget_usd ?? 3));
  const [autonomy, setAutonomy] = useState("supervised");
  const [models, setModels] = useState<Record<string, string>>({});
  const defaultModel = (role: string) => {
    const choice = config?.defaults?.models?.[role];
    return choice ? `${choice.provider}:${choice.model}` : "Server default";
  };
  const modelOptions = Object.entries(config?.models ?? {}).flatMap(([provider, names]) => Array.isArray(names) ? names.map((model) => ({ provider, model })) : []);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape" && !busy) onClose(); };
    document.addEventListener("keydown", onKey); return () => document.removeEventListener("keydown", onKey);
  }, [busy, onClose]);
  function submit(event: FormEvent) {
    event.preventDefault();
    const selected = Object.fromEntries(Object.entries(models).filter(([, value]) => value.trim()).map(([role, value]) => {
      const colon = value.indexOf(":"); return [role, { provider: value.slice(0, colon), model: value.slice(colon + 1) }];
    }));
    void onStart({ kind, title: title.trim(), body: body.trim(), ...(repo.trim() ? { repo: repo.trim() } : {}), budget_usd: Number(budget), autonomy, ...(Object.keys(selected).length ? { models: selected } : {}) }, idempotencyKey);
  }
  return <div className="modal-backdrop" onClick={() => !busy && onClose()}><form className="modal" role="dialog" aria-modal="true" aria-labelledby="scenario-title" onClick={(event) => event.stopPropagation()} onSubmit={submit}>
    <div className="section-heading"><div><span className="eyebrow">New run</span><h2 id="scenario-title">Start a scenario</h2></div><button type="button" className="icon-button" aria-label="Close dialog" disabled={busy} onClick={onClose}>×</button></div>
    <p className="muted">The server’s configured execution mode applies to this run. The mode is shown in its header and audit record.</p>
    <div className="field-grid"><label>Pipeline<select value={kind} onChange={(e) => setKind(e.target.value)}>{Object.entries(KIND).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label>Autonomy<select value={autonomy} onChange={(e) => setAutonomy(e.target.value)}><option value="supervised">Supervised · keep approval gates</option><option value="auto">Automatic · configured policy applies</option></select></label></div>
    <label>Scenario title<input autoFocus required maxLength={240} placeholder="Describe the change or review" value={title} onChange={(e) => setTitle(e.target.value)} /></label>
    <label>Details<textarea rows={4} placeholder="Context, expected behavior and acceptance criteria" value={body} onChange={(e) => setBody(e.target.value)} /></label>
    <div className="field-grid"><label>Repository<input placeholder="owner/repository" value={repo} onChange={(e) => setRepo(e.target.value)} /></label><label>Run budget (USD)<input type="number" min="0.01" max="25" step="0.01" required value={budget} onChange={(e) => setBudget(e.target.value)} /></label></div>
    <details className="model-options"><summary>Model settings</summary><p className="muted">Leave blank to use the shown default. Each run records its chosen models.</p><div className="field-grid three">{["planner", "coder", "reviewer"].map((role) => <label key={role}>{human(role)} model<input value={models[role] ?? ""} list="model-choices" pattern="(anthropic|openai|google):.+" placeholder={defaultModel(role)} onChange={(e) => setModels({ ...models, [role]: e.target.value })} /></label>)}</div><datalist id="model-choices">{modelOptions.map((option) => <option value={`${option.provider}:${option.model}`} key={`${option.provider}-${option.model}`} />)}</datalist></details>
    <div className="modal-actions"><button type="button" className="button secondary" disabled={busy} onClick={onClose}>Cancel</button><button className="button primary" type="submit" disabled={busy || !title.trim()}>{busy ? "Starting…" : "Start run →"}</button></div>
  </form></div>;
}

export function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const { data: overview, error: overviewError, loaded } = useOverview(refreshKey);
  const [runId, setRunId] = useState<string | null>(() => new URLSearchParams(location.search).get("run"));
  const { run: streamedRun, state: connection } = useRun(runId, refreshKey);
  const [tab, setTab] = useState("activity");
  const [view, setView] = useState("console");
  const [dialog, setDialog] = useState(false);
  const [authOpen, setAuthOpen] = useState(false);
  const [operatorToken, setOperatorToken] = useState(() => sessionStorage.getItem("sdlc-operator-token") ?? "");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [fault, setFault] = useState("worker_exit");
  const [point, setPoint] = useState("");
  const [command, setCommand] = useState("");
  const pendingCommand = useRef({ identity: "", key: "" });
  const [newVersion, setNewVersion] = useState("v1");
  const selected = (streamedRun?.run_id === runId ? streamedRun : null) ?? overview.runs.find((item) => item.run_id === runId) ?? null;
  const mode = selected?.mode ?? selected?.execution_mode ?? config?.mode ?? config?.execution_mode;
  const healthyWorkers = overview.workers.filter((worker) => worker.online !== false && !["offline", "stale", "dead", "stopped"].includes(worker.status ?? ""));
  const active = selected && !TERMINAL.has(selected.status);

  useEffect(() => { api<Config>("/api/config").then(setConfig).catch(() => undefined); }, [refreshKey]);
  useEffect(() => { if (!runId && overview.runs.length) setRunId(overview.runs[0].run_id); }, [runId, overview.runs]);
  useEffect(() => {
    const url = new URL(location.href); if (runId) url.searchParams.set("run", runId); else url.searchParams.delete("run");
    history.replaceState(null, "", url);
  }, [runId]);
  useEffect(() => { if (!notice) return; const timer = setTimeout(() => setNotice(null), 9000); return () => clearTimeout(timer); }, [notice]);
  useEffect(() => { setNewVersion(overview.version === "v2" ? "v2" : "v1"); }, [overview.version]);

  async function action(path: string, body: unknown, headers?: Record<string, string>): Promise<boolean> {
    setBusy(true);
    try { await api(path, { json: body, headers }); setRefreshKey((key) => key + 1); return true; }
    catch (error) { setNotice((error as Error).message); return false; }
    finally { setBusy(false); }
  }
  async function start(body: Record<string, unknown>, idempotencyKey: string) {
    setBusy(true);
    try {
      const response = await api<{ run_id?: string; run?: Run }>("/api/scenarios", { json: body, headers: { "Idempotency-Key": idempotencyKey } });
      setRunId(response.run_id ?? response.run?.run_id ?? null); setDialog(false); setView("console"); setTab("activity"); setRefreshKey((key) => key + 1);
    } catch (error) { setNotice((error as Error).message); }
    finally { setBusy(false); }
  }
  async function sendCommand(value: string) {
    if (!selected || !value.trim()) return;
    const identity = `${selected.run_id}:${value.trim()}`;
    if (pendingCommand.current.identity !== identity) pendingCommand.current = { identity, key: crypto.randomUUID() };
    if (await action(`/api/runs/${encodeURIComponent(selected.run_id)}/commands`, { command: value.trim() }, { "Idempotency-Key": pendingCommand.current.key })) {
      setCommand(""); pendingCommand.current = { identity: "", key: "" };
    }
  }

  return <div className="app-shell">
    <header className="topbar"><a className="brand" href="/" aria-label="SDLC Lab home"><span className="brand-mark">≋</span><div><strong>SDLC Lab</strong><span>PostgreSQL baseline</span></div></a>
      <nav className="top-nav" aria-label="Main navigation"><button aria-current={view === "console" ? "page" : undefined} onClick={() => setView("console")}>Run console</button><button aria-current={view === "evidence" ? "page" : undefined} onClick={() => setView("evidence")}>Evaluation</button></nav>
      <div className="topbar-right"><span className="runtime-tag">Application state machine</span><button className="icon-button" aria-label="Operator access" title="Operator access" onClick={() => setAuthOpen(!authOpen)}>⚙</button></div>
    </header>
    {authOpen && <form className="auth-bar" onSubmit={(e) => { e.preventDefault(); if (operatorToken) sessionStorage.setItem("sdlc-operator-token", operatorToken); else sessionStorage.removeItem("sdlc-operator-token"); setRefreshKey((key) => key + 1); setAuthOpen(false); }}><label>Operator token<input type="password" autoComplete="off" value={operatorToken} onChange={(e) => setOperatorToken(e.target.value)} placeholder="Only needed when the server requires it" /></label><button className="button secondary">Apply for this browser session</button></form>}
    <div className="workspace"><aside className="run-rail"><div className="rail-heading"><div><span className="eyebrow">Workspace</span><h2>Runs <span>{overview.runs.length}</span></h2></div><button className="button primary compact" onClick={() => setDialog(true)} aria-label="Start a scenario">＋ New</button></div>
      <div className="worker-summary"><span className={`connection-dot ${healthyWorkers.length ? "live" : "waiting"}`} /><strong>{healthyWorkers.length} runner{healthyWorkers.length === 1 ? "" : "s"} online</strong><span>{overview.version ?? config?.version ?? "—"}</span></div>
      <div className="run-list">{overview.runs.length ? overview.runs.map((item) => <button className="run-item" key={item.run_id} aria-current={runId === item.run_id ? "true" : undefined} onClick={() => { setRunId(item.run_id); setView("console"); }}><div><span className="eyebrow">{KIND[item.kind] ?? human(item.kind)}</span><span className="mono">{item.version}</span></div><strong>{item.title}</strong><div><Badge value={item.status} /><span className="run-cost">{money(item.usage?.cost_usd)}</span></div></button>) : <div className="rail-empty"><span className="empty-symbol">↗</span><p>{loaded ? "No runs yet" : "Connecting to the API…"}</p><span>Start a scenario to create the first durable run.</span></div>}</div>
      <div className="rail-foot"><span className="eyebrow">Durable store</span><strong>PostgreSQL</strong><p>Progress, gates, timers and audit events survive pod restarts.</p></div>
    </aside><main className="main-content">
      {overviewError && <div className="notice error" role="alert"><strong>API unavailable.</strong> {overviewError} <button onClick={() => setRefreshKey((key) => key + 1)}>Retry</button></div>}
      {!healthyWorkers.length && !overviewError && <div className="notice neutral"><span className="connection-dot waiting" /><strong>No runner is currently reporting healthy.</strong> Saved runs remain visible; queued work will wait for a compatible runner.</div>}
      {view === "evidence" ? <EvidencePanel run={selected} /> : selected ? <>
        <div className="run-heading"><div><div className="eyebrow">{KIND[selected.kind] ?? human(selected.kind)} <span> / {selected.version}</span></div><h1>{selected.title}</h1><div className="run-metadata"><Badge value={selected.status} /><span className="mode-tag" data-mode={mode}>{mode ? `${human(mode)} execution` : "Execution mode unavailable"}</span><span className="mono run-id" title={selected.run_id}>{selected.run_id}</span><span className="stream-state"><span className={`connection-dot ${connection === "live" ? "live" : "waiting"}`} />{human(connection)}</span></div></div>
          <div className="spend"><span className="eyebrow">Recorded spend</span><strong>{money(selected.usage?.cost_usd)}<small> / {money(selected.budget_usd)}</small></strong><div className="budget-track"><span style={{ width: `${Math.min(100, ((selected.usage?.cost_usd ?? 0) / (selected.budget_usd || 1)) * 100)}%` }} /></div><span>{selected.usage?.requests ?? 0} requests · {((selected.usage?.input_tokens ?? 0) + (selected.usage?.output_tokens ?? 0)).toLocaleString()} tokens</span></div>
        </div>
        {mode === "scripted" && <div className="notice scripted"><strong>Scripted execution.</strong> Outputs, provider usage and release actions are simulated fixtures. Use live runs to evaluate real external effects.</div>}
        <Pipeline steps={selected.steps ?? []} />
        {selected.gate && <GatePanel gate={selected.gate} busy={busy} onCommand={(value) => void sendCommand(value)} />}
        <section className="card detail-card"><div className="tabbar" role="tablist" aria-label="Run detail">{[["activity", "Agent activity"], ["conversation", "Conversation"], ["history", "Audit history"], ["attempts", "Attempts"], ["release", "Branch & release"]].map(([key, label]) => <button key={key} role="tab" aria-selected={tab === key} onClick={() => setTab(key)}>{label}{key === "history" && <span>{selected.events?.length ?? selected.history?.length ?? 0}</span>}</button>)}</div>
          <div className="tab-content">{tab === "activity" && <AgentActivity run={selected} />}{tab === "conversation" && <ActivityList items={selected.conversation ?? []} empty="No conversation has been recorded for this run." />}{tab === "history" && <ActivityList items={selected.events ?? selected.history ?? []} empty="No audit events are available yet." />}{tab === "attempts" && <ActivityList items={attemptsFor(selected)} empty="No separate attempt records are available yet. Stage attempt numbers are shown above." />}{tab === "release" && <ReleasePanel run={selected} environment={overview.environments} />}</div>
          {tab === "conversation" && <form className="command-form" onSubmit={(e) => { e.preventDefault(); void sendCommand(command); }}><label className="sr-only" htmlFor="command">Command or feedback</label><input id="command" placeholder="Send feedback or a command, such as /approve" value={command} onChange={(e) => setCommand(e.target.value)} disabled={busy || !active} /><button className="button primary" disabled={busy || !active || !command.trim()}>Send →</button></form>}
        </section>
        <section className="card controls-card"><div><span className="eyebrow">Failure injection</span><p>Faults apply to this run. Each injection is recorded for later inspection.</p></div><div className="fault-controls"><label className="sr-only" htmlFor="fault">Fault type</label><select id="fault" value={fault} onChange={(e) => setFault(e.target.value)}>{FAULTS.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select><label className="sr-only" htmlFor="fault-point">Fault point</label><select id="fault-point" value={point} onChange={(e) => setPoint(e.target.value)}><option value="">Default fault boundary</option><option value="before_task">Before task</option><option value="after_effect">After external effect</option><option value="model">Before model request</option><option value="executor">Before executor work</option><option value="canary">During canary check</option></select><button className="button secondary" disabled={busy || !active} onClick={() => void action(`/api/runs/${encodeURIComponent(selected.run_id)}/faults`, { kind: fault, ...(point ? { point } : {}) })}>Inject fault</button></div></section>
      </> : <section className="welcome"><span className="eyebrow">Agentic SDLC · Baseline implementation</span><h1>Follow a change from<br />issue to release.</h1><p>Inspect agent work, review decisions and restart behavior in an application whose progress is saved in PostgreSQL.</p><button className="button primary large" onClick={() => setDialog(true)}>Start the first scenario →</button><div className="welcome-stages"><span>Plan</span><i>→</i><span>Code in parallel</span><i>→</i><span>Review</span><i>→</i><span>Release</span></div><span className="welcome-note">The console remains available when runners are offline.</span></section>}
    </main></div>
    <footer className="footer"><div><span className={`connection-dot ${loaded && !overviewError ? "live" : "waiting"}`} /><strong>PostgreSQL baseline</strong><span>Durable transitions · at-least-once task attempts</span></div><form onSubmit={(e) => { e.preventDefault(); void action("/api/version", { version: newVersion }); }}><label htmlFor="version">New-run version</label><select id="version" value={newVersion} onChange={(e) => setNewVersion(e.target.value)}><option>v1</option><option>v2</option></select><button type="submit" disabled={busy || newVersion === overview.version}>Apply</button></form></footer>
    {dialog && <ScenarioDialog config={config} busy={busy} onClose={() => setDialog(false)} onStart={start} />}
    {notice && <div className="toast" role="alert"><span>{notice}</span><button aria-label="Dismiss notification" onClick={() => setNotice(null)}>×</button></div>}
  </div>;
}
