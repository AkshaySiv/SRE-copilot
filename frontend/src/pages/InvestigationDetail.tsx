import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  AlertTriangle, ArrowLeft, Bot, CheckCircle2, CircleSlash, FileSearch, FlaskConical, Play, Send, ShieldCheck, Square, Wrench, XCircle,
} from "lucide-react";
import { api, streamInvestigation, timeAgo } from "../api";
import { useMe } from "../App";
import type { InvEvent, InvestigationDetail as Detail, Remediation, Report } from "../types";
import { Empty, ErrorBox, Markdown, RiskPill, Spinner, useAsync } from "../components/ui";
import { StatusPill } from "./Investigations";

const PHASES: Record<string, string> = {
  starting: "Connecting to your integrations…", investigating: "Investigating…",
  writing_report: "Writing the incident report…", writing_answer: "Writing the answer…",
};

function ToolStep({ call, result }: { call: InvEvent; result?: InvEvent }) {
  const ok = result?.data.ok;
  const cls = !result ? "run" : ok ? "ok" : "err";
  return (
    <details className={`step ${cls}`}>
      <summary className="row" style={{ width: "100%" }}>
        <span className="step-icon">{!result ? <Spinner size={13} /> : ok ? <CheckCircle2 size={14} /> : <XCircle size={14} />}</span>
        <span className="grow">{call.data.label}</span>
        <span className="muted small">{call.data.integration}{result ? ` · ${(result.data.duration_ms / 1000).toFixed(1)}s` : ""}</span>
      </summary>
      <div style={{ marginTop: 8, marginLeft: 34 }}>
        <div className="muted small mono">{call.data.tool}({JSON.stringify(call.data.args)})</div>
        {result && <pre className="code logs" style={{ marginTop: 6, maxHeight: 320 }}>{result.data.output}</pre>}
      </div>
    </details>
  );
}

function Timeline({ events, running }: { events: InvEvent[]; running: boolean }) {
  const results = useMemo(() => new Map(events.filter((e) => e.type === "tool_result").map((e) => [e.data.id, e])), [events]);
  const lastPhase = [...events].reverse().find((e) => e.type === "status")?.data.phase;
  return (
    <div className="timeline">
      {events.map((e) => {
        switch (e.type) {
          case "user_message": return <div key={e.seq} className="msg-user">{e.data.text}</div>;
          case "thinking": return <div key={e.seq} className="thinking"><Bot size={13} style={{ verticalAlign: -2 }} /> {e.data.text}</div>;
          case "tool_call": return <ToolStep key={e.seq} call={e} result={results.get(e.data.id)} />;
          case "answer": return <div key={e.seq} className="answer"><Markdown>{e.data.markdown}</Markdown></div>;
          case "report": return (
            <div key={e.seq} className="answer" style={{ padding: 14, borderColor: "var(--accent)" }}>
              <div className="row"><FileSearch size={16} style={{ color: "var(--accent)" }} /><b>Incident report ready</b></div>
              <p className="text-2" style={{ margin: "6px 0 0" }}>{e.data.report?.summary}</p>
            </div>
          );
          case "error": return <ErrorBox key={e.seq} error={e.data.message} />;
          case "done": return (
            <div key={e.seq} className="muted small" style={{ textAlign: "center" }}>
              {e.data.status === "completed" ? `Finished · ${e.data.tool_calls ?? 0} checks` : `Stopped (${e.data.status})`}
              {e.data.usage?.total_tokens ? ` · ${e.data.usage.total_tokens.toLocaleString()} tokens` : ""}
            </div>
          );
          default: return null;
        }
      })}
      {running && <div className="row muted pulse"><Spinner size={14} /> {PHASES[lastPhase] ?? "Working…"}</div>}
    </div>
  );
}

function RemediationCard({ rem, onChange }: { rem: Remediation; onChange: (r: Remediation) => void }) {
  const me = useMe();
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<unknown>();
  const canAct = me && me.role !== "viewer" && me.remediation_mode !== "disabled";
  const call = async (what: "dry-run" | "approve" | "reject") => {
    if (what === "approve" && !window.confirm(`Execute "${rem.title}" now?\n\n${rem.plan.summary ?? ""}`)) return;
    setBusy(what); setErr(undefined);
    try { onChange(await api<Remediation>(`/remediations/${rem.id}/${what}`, { body: what === "reject" ? { reason: "" } : {} })); }
    catch (e) { setErr(e); } finally { setBusy(null); }
  };
  const statusLabel: Record<string, string> = {
    proposed: "Proposed", dry_run_ok: "Dry-run passed", dry_run_failed: "Dry-run failed", executed: "Executed",
    failed: "Execution failed", rejected: "Rejected",
  };
  return (
    <div className="rem-card">
      <div className="row"><Wrench size={15} /><b className="grow">{rem.title}</b><RiskPill risk={rem.risk} /></div>
      <div className="small text-2" style={{ margin: "6px 0" }}>{rem.plan.summary}</div>
      <dl className="kv small">
        {rem.plan.changes?.length ? <><dt>Changes</dt><dd>{rem.plan.changes.join("; ")}</dd></> : null}
        {rem.plan.impact && <><dt>Impact</dt><dd>{rem.plan.impact}</dd></>}
        {rem.plan.rollback && <><dt>Undo</dt><dd>{rem.plan.rollback}</dd></>}
        <dt>Action</dt><dd className="mono">{rem.action}({Object.entries(rem.params).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ")})</dd>
        <dt>Status</dt><dd><b>{statusLabel[rem.status] ?? rem.status}</b>{rem.decided_by ? ` by ${rem.decided_by}` : ""}</dd>
      </dl>
      {rem.dry_run && (
        <div className="small" style={{ marginTop: 8 }}>
          <b style={{ color: rem.dry_run.ok ? "var(--healthy)" : "var(--critical)" }}>{rem.dry_run.ok ? "✓ Server-side dry-run succeeded" : "✗ Dry-run failed"}</b>
          <pre className="code" style={{ marginTop: 4, maxHeight: 140 }}>{JSON.stringify(rem.dry_run.output ?? rem.dry_run.error, null, 2)}</pre>
        </div>
      )}
      {rem.result && rem.status !== "rejected" && (
        <div className="small" style={{ marginTop: 8 }}>
          <b style={{ color: rem.result.ok ? "var(--healthy)" : "var(--critical)" }}>{rem.result.ok ? "✓ Applied" : "✗ Failed"}</b>
          <pre className="code" style={{ marginTop: 4, maxHeight: 140 }}>{JSON.stringify(rem.result.output ?? rem.result.error, null, 2)}</pre>
        </div>
      )}
      <ErrorBox error={err} />
      {canAct && !["executed", "rejected"].includes(rem.status) && (
        <div className="row wrap" style={{ marginTop: 10 }}>
          <button className="btn sm" disabled={!!busy} onClick={() => call("dry-run")}>{busy === "dry-run" ? <Spinner size={12} /> : <FlaskConical size={13} />} Dry run</button>
          <button className="btn sm primary" disabled={!!busy || rem.status !== "dry_run_ok"} title={rem.status !== "dry_run_ok" ? "Run a successful dry-run first" : ""}
            onClick={() => call("approve")}>{busy === "approve" ? <Spinner size={12} /> : <Play size={13} />} Approve & execute</button>
          <button className="btn sm ghost danger" disabled={!!busy} onClick={() => call("reject")}><CircleSlash size={13} /> Reject</button>
        </div>
      )}
      {!canAct && rem.status === "proposed" && <div className="muted small" style={{ marginTop: 8 }}>An operator must review and approve this action.</div>}
    </div>
  );
}

function ReportPanel({ report, remediations, onRemChange }: { report: Report; remediations: Remediation[]; onRemChange: (r: Remediation) => void }) {
  const remByTitle = new Map(remediations.map((r) => [r.title, r]));
  const orphan = remediations.filter((r) => !report.recommendations.some((x) => x.title === r.title));
  const rc = report.root_cause;
  return (
    <div className="panel" style={{ position: "sticky", top: 16 }}>
      <div className="report-section">
        <div className="row"><span className={`sev-${report.severity}`} style={{ fontWeight: 700, textTransform: "uppercase", fontSize: 12 }}>{report.severity}</span>
          <span className="muted small">· {report.status.replace(/_/g, " ")}</span></div>
        <h2 style={{ marginTop: 4 }}>{report.title}</h2>
        <p className="text-2" style={{ marginBottom: 0 }}>{report.summary}</p>
        {report.impact && <p className="small" style={{ marginBottom: 0 }}><b>Impact:</b> {report.impact}</p>}
      </div>
      {rc && (
        <div className="report-section">
          <h3>Root cause</h3>
          <p style={{ margin: "0 0 8px" }}>{rc.description}</p>
          <div className="row small muted"><span className="chip">{rc.category}</span><span className="grow" />Confidence {(rc.confidence * 100).toFixed(0)}%</div>
          <div className="confidence" style={{ marginTop: 6 }}><div style={{ width: `${rc.confidence * 100}%` }} /></div>
        </div>
      )}
      {(report.recommendations.length > 0 || orphan.length > 0) && (
        <div className="report-section">
          <h3>Recommended actions</h3>
          {report.recommendations.map((r, i) => (
            <div key={i} style={{ marginTop: i ? 12 : 0 }}>
              <div className="row"><b className="grow">{i + 1}. {r.title}</b><span className="chip">{r.kind.replace("_", " ")}</span></div>
              <div className="small text-2" style={{ whiteSpace: "pre-wrap" }}>{r.description}</div>
              {remByTitle.get(r.title) && <RemediationCard rem={remByTitle.get(r.title)!} onChange={onRemChange} />}
            </div>
          ))}
          {orphan.map((r) => <RemediationCard key={r.id} rem={r} onChange={onRemChange} />)}
        </div>
      )}
      {report.evidence.length > 0 && (
        <div className="report-section">
          <h3>Evidence</h3>
          {report.evidence.map((e, i) => (
            <div key={i} className="evidence small"><b>{e.finding}</b> <span className="muted">— {e.source}</span>{e.detail && <code>{e.detail}</code>}</div>
          ))}
        </div>
      )}
      {report.timeline.length > 0 && (
        <div className="report-section"><h3>Timeline</h3>
          {report.timeline.map((t, i) => <div key={i} className="tl-item"><span className="muted mono">{t.time}</span><span>{t.event}</span></div>)}
        </div>
      )}
      {[["Contributing factors", report.contributing_factors], ["Also noticed", report.other_findings], ["Open questions", report.open_questions]]
        .filter(([, items]) => (items as string[]).length > 0).map(([title, items]) => (
          <div key={title as string} className="report-section"><h3>{title as string}</h3>
            <ul className="kbd-list small">{(items as string[]).map((x, i) => <li key={i}>{x}</li>)}</ul></div>
        ))}
    </div>
  );
}

export default function InvestigationDetail() {
  const { id } = useParams<{ id: string }>();
  const detail = useAsync(() => api<Detail>(`/investigations/${id}`), [id]);
  const [events, setEvents] = useState<InvEvent[]>([]);
  const [rems, setRems] = useState<Remediation[]>([]);
  const [report, setReport] = useState<Report | null>(null);
  const [running, setRunning] = useState(false);
  const [followUp, setFollowUp] = useState("");
  const [sendErr, setSendErr] = useState<unknown>();
  const bottom = useRef<HTMLDivElement>(null);
  const [streamKey, setStreamKey] = useState(0);

  useEffect(() => {
    const d = detail.data;
    if (!d) return;
    setEvents(d.events); setRems(d.remediations); setReport(d.report);
    setRunning(d.running || d.status === "running" || d.status === "queued");
  }, [detail.data]);

  useEffect(() => {
    const d = detail.data;
    if (!d || !(d.running || d.status === "queued" || d.status === "running")) return;
    const after = d.events.length ? d.events[d.events.length - 1].seq : 0;
    return streamInvestigation(d.id, Math.max(after, ...events.map((e) => e.seq), 0), (ev) => {
      setEvents((prev) => prev.some((p) => p.seq === ev.seq) ? prev : [...prev, ev]);
      if (ev.type === "report") setReport(ev.data.report);
      if (ev.type === "remediation") detail.reload(true);
      if (ev.type === "remediation_update") setRems((prev) => prev.map((r) => r.id === ev.data.id ? ev.data : r));
      if (ev.type === "done") { setRunning(false); detail.reload(true); }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail.data?.id, streamKey]);

  useEffect(() => { if (running) bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [events.length, running]);

  if (detail.loading && !detail.data) return <div className="page"><Spinner /></div>;
  if (detail.error) return <div className="page"><ErrorBox error={detail.error} /></div>;
  const d = detail.data!;

  const send = async () => {
    setSendErr(undefined);
    try {
      await api(`/investigations/${d.id}/messages`, { body: { prompt: followUp } });
      setFollowUp(""); setRunning(true);
      await detail.reload(true);
      setStreamKey((k) => k + 1);
    } catch (e) { setSendErr(e); }
  };
  const updateRem = (r: Remediation) => setRems((prev) => prev.map((x) => x.id === r.id ? r : x));
  const showSide = d.kind === "incident" || !!report;

  return (
    <div className="page">
      <div className="page-head">
        <div className="grow">
          <Link to="/investigations" className="small row" style={{ gap: 4 }}><ArrowLeft size={13} /> Investigations</Link>
          <h1 style={{ marginTop: 6 }}>{report?.title ?? d.title}</h1>
          <div className="row small muted" style={{ marginTop: 6 }}>
            <StatusPill status={running ? "running" : d.status} running={running} />
            <span>{d.kind === "incident" ? "Incident investigation" : "Question"}</span>·<span>by {d.created_by}</span>·<span>{timeAgo(d.created_at)}</span>
            {d.scope?.namespace ? <><span>·</span><span className="chip">ns: {String(d.scope.namespace)}</span></> : null}
          </div>
        </div>
        {running && <button className="btn danger" onClick={() => api(`/investigations/${d.id}/cancel`, { body: {} })}><Square size={13} /> Stop</button>}
      </div>
      <div className={showSide ? "inv-layout" : ""} style={showSide ? {} : { maxWidth: 900, margin: "0 auto" }}>
        <div className="col">
          <Timeline events={events} running={running} />
          {d.error && !running && <ErrorBox error={d.error} />}
          <div ref={bottom} />
          <div className="composer">
            <form className="ask-inner" onSubmit={(e) => { e.preventDefault(); if (followUp.trim() && !running) send(); }}>
              <input className="input" disabled={running} placeholder={running ? "The copilot is working…" : "Ask a follow-up question…"} value={followUp} onChange={(e) => setFollowUp(e.target.value)} />
              <button className="btn primary" disabled={running || !followUp.trim()}><Send size={14} /> Send</button>
            </form>
            <ErrorBox error={sendErr} />
          </div>
        </div>
        {showSide && (
          <div>
            {report ? <ReportPanel report={report} remediations={rems} onRemChange={updateRem} /> : (
              <div className="panel">
                <Empty>
                  {running ? <><Spinner /> <div style={{ marginTop: 8 }}>The report appears here when the investigation completes.</div></>
                    : d.status === "failed" ? <><AlertTriangle /> <div>Investigation failed.</div></> : "No report."}
                </Empty>
              </div>
            )}
            <div className="row small muted" style={{ marginTop: 10, gap: 6 }}><ShieldCheck size={14} /> Investigations are read-only. Changes run only after a dry-run and your approval, and are audit-logged.</div>
          </div>
        )}
      </div>
    </div>
  );
}
