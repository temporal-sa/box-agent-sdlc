import { useEffect, useRef, useState } from "react";
import { api, operatorHeaders, unwrapRun, type Overview, type Run } from "./api";
import { SSEParser } from "./sse";

export function useOverview(refreshKey: number) {
  const [data, setData] = useState<Overview>({ runs: [], workers: [] });
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    async function poll() {
      try {
        const [snapshot, environment] = await Promise.allSettled([
          api<Overview>("/api/overview", { signal: controller.signal }),
          api<Record<string, unknown>>("/api/environments", { signal: controller.signal }),
        ]);
        if (snapshot.status === "rejected") throw snapshot.reason;
        const next = snapshot.value;
        setData((previous) => ({ ...next, runs: next.runs ?? [], workers: next.workers ?? [], environments: environment.status === "fulfilled" ? environment.value : previous.environments }));
        setError(null);
      } catch (e) { if (!controller.signal.aborted) setError((e as Error).message); }
      finally { if (!controller.signal.aborted) setLoaded(true); }
    }
    void poll();
    const timer = setInterval(() => void poll(), 3000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [refreshKey]);
  return { data, error, loaded };
}

export function useRun(runId: string | null, refreshKey: number) {
  const [run, setRun] = useState<Run | null>(null);
  const [state, setState] = useState("connecting");
  const cursor = useRef({ runId: "", id: "" });
  useEffect(() => {
    if (!runId) { setRun(null); setState("idle"); return; }
    const controller = new AbortController();
    let reconnect: ReturnType<typeof setTimeout>;
    let retry = 1000;
    if (cursor.current.runId !== runId) { cursor.current = { runId, id: "" }; setRun(null); }
    const apply = (value: Run) => setRun((previous) => {
      if (previous?.run_id !== value.run_id) return value;
      if (Number(previous.revision ?? 0) > Number(value.revision ?? 0)) return previous;
      // SSE snapshots omit the detailed ledger; retain it until the next detail poll.
      return { ...previous, ...value };
    });
    async function refresh() {
      try {
        const value = unwrapRun(await api<unknown>(`/api/runs/${encodeURIComponent(runId!)}`, { signal: controller.signal }));
        if (value && !controller.signal.aborted) apply(value);
      } catch { /* The overview and connection indicator expose availability. */ }
    }
    async function stream() {
      if (controller.signal.aborted) return;
      setState("connecting");
      await refresh();
      try {
        const response = await fetch(`/api/runs/${encodeURIComponent(runId!)}/stream`, {
          headers: { ...operatorHeaders(), Accept: "text/event-stream", ...(cursor.current.id ? { "Last-Event-ID": cursor.current.id } : {}) },
          signal: controller.signal, credentials: "same-origin",
        });
        if (!response.ok || !response.body) throw new Error("Stream unavailable");
        setState("live");
        retry = 1000;
        const parser = new SSEParser((event) => {
          if (event.id !== undefined) cursor.current.id = event.id;
          try {
            const value = unwrapRun(JSON.parse(event.data));
            if (value) apply(value); else void refresh();
          } catch { void refresh(); }
        });
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        while (!controller.signal.aborted) {
          const { done, value } = await reader.read();
          if (done) break;
          parser.feed(decoder.decode(value, { stream: true }));
        }
      } catch { /* Reconnect below, keeping the last committed view and event cursor. */ }
      if (!controller.signal.aborted) {
        setState("reconnecting");
        reconnect = setTimeout(() => void stream(), retry);
        retry = Math.min(retry * 2, 10000);
      }
    }
    void stream();
    const poll = setInterval(() => void refresh(), 5000);
    return () => { controller.abort(); clearTimeout(reconnect); clearInterval(poll); };
  }, [runId, refreshKey]);
  return { run, state };
}
