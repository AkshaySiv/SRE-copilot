import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronDown, ChevronRight, Search, Siren } from "lucide-react";
import { api, timeAgo } from "../api";
import { startInvestigation } from "../lib";
import type { Investigation } from "../types";
import { ScopePicker, type Scope } from "../components/ScopePicker";
import { Empty, ErrorBox, HealthPill, Spinner, useAsync } from "../components/ui";

const STATUS_HEALTH: Record<string, string> = { completed: "healthy", running: "unknown", queued: "unknown", failed: "critical", cancelled: "warning" };

export function StatusPill({ status, running }: { status: string; running?: boolean }) {
  return <HealthPill health={STATUS_HEALTH[status] ?? "unknown"} label={running ? "running" : status} />;
}

export default function Investigations() {
  const navigate = useNavigate();
  const [prompt, setPrompt] = useState("");
  const [scope, setScope] = useState<Scope>({});
  const [showScope, setShowScope] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>();
  const [q, setQ] = useState("");
  const [kind, setKind] = useState("");
  const list = useAsync(() => api<{ total: number; items: Investigation[] }>(`/investigations?limit=100${q ? `&q=${encodeURIComponent(q)}` : ""}${kind ? `&kind=${kind}` : ""}`), [q, kind], 8000);

  const submit = async () => {
    setBusy(true); setErr(undefined);
    try { await startInvestigation(navigate, { kind: "incident", prompt, scope: scope as Record<string, unknown> }); }
    catch (e) { setErr(e); } finally { setBusy(false); }
  };

  return (
    <div className="page">
      <div className="page-head"><div><h1>Investigations</h1><p>Describe a problem in plain words. The copilot gathers evidence, finds the root cause and proposes a safe fix.</p></div></div>
      <div className="panel hero-ask col" style={{ marginBottom: 20 }}>
        <textarea className="textarea" placeholder="e.g. Customers report checkout is failing since about 20 minutes ago" value={prompt} onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && prompt.trim()) submit(); }} />
        <div className="row wrap">
          <button className="btn ghost sm" onClick={() => setShowScope(!showScope)}>{showScope ? <ChevronDown size={14} /> : <ChevronRight size={14} />} Scope & model</button>
          <span className="spacer" />
          <span className="muted small">Read-only investigation · fixes need your approval</span>
          <button className="btn primary" disabled={busy || prompt.trim().length < 3} onClick={submit}>{busy ? <Spinner size={14} /> : <Siren size={15} />} Start investigation</button>
        </div>
        {showScope && <ScopePicker value={scope} onChange={setScope} />}
        <ErrorBox error={err} />
      </div>

      <div className="row" style={{ marginBottom: 10 }}>
        <div className="row grow" style={{ maxWidth: 360, background: "var(--bg-2)", border: "1px solid var(--border-2)", borderRadius: 8, padding: "0 10px" }}>
          <Search size={14} className="muted" /><input className="input" style={{ border: 0, background: "transparent" }} placeholder="Search past incidents and questions" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <select className="select" style={{ width: 160 }} value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">All types</option><option value="incident">Incidents</option><option value="question">Questions</option>
        </select>
        <span className="muted small">{list.data?.total ?? 0} total</span>
      </div>
      <div className="panel">
        <ErrorBox error={list.error} />
        {list.loading && !list.data ? <div className="panel-pad"><Spinner /></div> : !list.data?.items.length ? <Empty>Nothing here yet.</Empty> : (
          <table className="table">
            <thead><tr><th>Status</th><th>What</th><th>Severity</th><th>Root cause / answer</th><th>When</th></tr></thead>
            <tbody>
              {list.data.items.map((i) => (
                <tr key={i.id} className="clickable" onClick={() => navigate(`/investigations/${i.id}`)}>
                  <td><StatusPill status={i.status} running={i.running} /></td>
                  <td style={{ maxWidth: 380 }}><div><b>{i.title}</b></div><div className="muted small">{i.kind} · by {i.created_by}</div></td>
                  <td>{i.severity ? <b className={`sev-${i.severity}`} style={{ textTransform: "capitalize" }}>{i.severity}</b> : <span className="muted">—</span>}</td>
                  <td className="small text-2" style={{ maxWidth: 460 }}>{(i.root_cause ?? i.summary ?? i.error ?? "").slice(0, 220)}</td>
                  <td className="small muted" style={{ whiteSpace: "nowrap" }}>{timeAgo(i.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
