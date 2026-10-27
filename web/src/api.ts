export interface Step {
  key: string; label: string; status: string; detail?: string; lane?: string | null;
  attempt?: number; started_at?: string | number | null; ended_at?: string | number | null;
}
export interface Gate {
  key?: string; question?: string; context?: string; commands?: string[];
  questions?: string[]; closes_at?: string | number | null;
}
export interface Usage {
  cost_usd?: number; input_tokens?: number; output_tokens?: number; requests?: number;
}
export interface Activity {
  id?: string | number; at?: string | number; timestamp?: string | number; type?: string;
  role?: string; author?: string; text?: string; body?: string; message?: string;
  detail?: string; status?: string; outcome?: string; model?: string; lane?: string;
  attempt?: number; cost_usd?: number; [key: string]: unknown;
}
export interface Run {
  run_id: string; kind: string; version: string; title: string; status: string;
  steps?: Step[]; gate?: Gate | null; usage?: Usage; budget_usd?: number;
  conversation?: Activity[]; agents?: Activity[]; events?: Activity[]; history?: Activity[];
  attempts?: Activity[]; started_at?: string | number; updated_at?: string | number;
  issue_url?: string | null; pr_url?: string | null; branch?: string; branch_name?: string;
  staging_sha?: string | null; canary_sha?: string | null; stable_sha?: string | null;
  release?: Record<string, unknown>; mode?: string; execution_mode?: string;
  [key: string]: unknown;
}
export interface Worker {
  executor_id?: string; worker_id?: string; version?: string; status?: string;
  last_seen_at?: string | number; online?: boolean; [key: string]: unknown;
}
export interface Config {
  repo?: string; models?: Record<string, string[]>; budget_usd?: number; mode?: string;
  execution_mode?: string; version?: string; github_repo?: string;
  defaults?: { budget_usd?: number }; release_semantics?: string; [key: string]: unknown;
}
export interface Overview { runs: Run[]; workers: Worker[]; version?: string; environments?: Record<string, unknown>; [key: string]: unknown }

export function operatorHeaders(): Record<string, string> {
  const token = sessionStorage.getItem("sdlc-operator-token");
  return token ? { "X-Operator-Token": token } : {};
}

export async function api<T>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const response = await fetch(path, {
    ...init,
    method: init?.method ?? (init?.json === undefined ? "GET" : "POST"),
    headers: { ...operatorHeaders(), ...(init?.json === undefined ? {} : { "Content-Type": "application/json" }), ...init?.headers },
    body: init?.json === undefined ? init?.body : JSON.stringify(init.json),
    credentials: "same-origin",
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    const body = await response.text();
    try {
      const value = JSON.parse(body);
      detail = typeof value.detail === "string" ? value.detail : JSON.stringify(value.detail ?? value);
    } catch { if (body) detail = body; }
    throw new Error(detail);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function unwrapRun(value: unknown): Run | null {
  if (!value || typeof value !== "object") return null;
  const obj = value as Record<string, unknown>;
  const candidate = obj.run && typeof obj.run === "object" ? obj.run as Record<string, unknown> : obj;
  return typeof candidate.run_id === "string" && typeof candidate.status === "string" ? candidate as unknown as Run : null;
}

export const money = (value?: number) => `$${Number(value ?? 0).toFixed(3)}`;
export const human = (value?: string) => (value ?? "pending").replaceAll("_", " ");
export const shortSha = (value?: string | null) => value ? value.slice(0, 8) : "—";
export function timestamp(value?: string | number | null): string {
  if (!value) return "—";
  const date = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
