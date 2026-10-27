import type { InvEvent } from "./types";

const TOKEN_KEY = "srecopilot.token";

export const tokenStore = {
  get: (): string | null => { try { return localStorage.getItem(TOKEN_KEY); } catch { return null; } },
  set: (t: string | null) => { try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch { /* storage unavailable */ } },
};

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

function headers(json = true): Record<string, string> {
  const h: Record<string, string> = json ? { "Content-Type": "application/json" } : {};
  const t = tokenStore.get();
  if (t) h.Authorization = `Bearer ${t}`;
  return h;
}

function detailText(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d: any) => `${(d.loc ?? []).slice(-1)[0] ?? ""}: ${d.msg}`).join("; ");
  return JSON.stringify(detail);
}

export async function api<T = unknown>(path: string, init: { method?: string; body?: unknown; signal?: AbortSignal } = {}): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method: init.method ?? (init.body !== undefined ? "POST" : "GET"),
    headers: headers(), body: init.body !== undefined ? JSON.stringify(init.body) : undefined, signal: init.signal,
  });
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  const data = text ? JSON.parse(text) : undefined;
  if (!res.ok) throw new ApiError(res.status, data?.detail ? detailText(data.detail) : res.statusText);
  return data as T;
}

/** Server-sent events over fetch (so the bearer token can be sent). Reconnects from the last seq until done. */
export function streamInvestigation(id: string, after: number, onEvent: (e: InvEvent) => void): () => void {
  const ctrl = new AbortController();
  let last = after;
  let finished = false;

  const run = async () => {
    while (!ctrl.signal.aborted && !finished) {
      try {
        const res = await fetch(`/api/investigations/${id}/stream?after=${last}`, { headers: headers(false), signal: ctrl.signal });
        if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
          let idx;
          while ((idx = buf.indexOf("\n\n")) >= 0) {
            const chunk = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            const evType = /^event: ?(.*)$/m.exec(chunk)?.[1];
            const data = chunk.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trimStart()).join("\n");
            if (!data || evType === "ping") continue;
            const ev = JSON.parse(data) as InvEvent;
            if (ev.seq != null) last = Math.max(last, ev.seq);
            onEvent(ev);
            if (ev.type === "done") finished = true;
          }
        }
      } catch (err) {
        if (ctrl.signal.aborted) return;
      }
      if (!finished) await new Promise((r) => setTimeout(r, 1500));
    }
  };
  run();
  return () => ctrl.abort();
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
