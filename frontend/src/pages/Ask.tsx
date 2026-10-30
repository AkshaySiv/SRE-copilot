import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronDown, ChevronRight, MessageSquareText, Send, Siren } from "lucide-react";
import { api, timeAgo } from "../api";
import { classifyPrompt, startInvestigation } from "../lib";
import type { Investigation } from "../types";
import { ScopePicker, type Scope } from "../components/ScopePicker";
import { ErrorBox, Spinner, useAsync } from "../components/ui";

const SUGGESTIONS = [
  { group: "Health", items: ["Is anything broken in the cluster right now?", "Which pods restarted the most today, and why?", "Are any nodes under memory or disk pressure?"] },
  { group: "Capacity", items: ["Which workloads are close to their memory limits?", "Are any persistent volumes almost full?", "Which services have no ready endpoints?"] },
  { group: "Changes", items: ["What was deployed in the last few hours?", "Did any rollout fail recently?", "Which images are running old versions?"] },
  { group: "Servers", items: ["Is disk space OK on all my VMs?", "Are there failed systemd services on any VM?", "Which VMs have high load right now?"] },
];

export default function Ask() {
  const navigate = useNavigate();
  const [text, setText] = useState("");
  const [scope, setScope] = useState<Scope>({});
  const [showScope, setShowScope] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>();
  const recent = useAsync(() => api<{ items: Investigation[] }>("/investigations?kind=question&limit=8"), []);

  const ask = async (prompt: string) => {
    if (!prompt.trim()) return;
    setBusy(true); setErr(undefined);
    try { await startInvestigation(navigate, { kind: "question", prompt, scope: scope as Record<string, unknown> }); }
    catch (e) { setErr(e); setBusy(false); }
  };

  return (
    <div className="page" style={{ maxWidth: 980 }}>
      <div className="page-head"><div><h1>Ask the copilot</h1><p>Ask about your infrastructure in plain language — no kubectl or shell commands needed. Describing a failure starts an incident timeline automatically.</p></div></div>
      <form className="panel hero-ask col" onSubmit={(e) => { e.preventDefault(); ask(text); }}>
        <textarea className="textarea" autoFocus placeholder="e.g. Why is the payments service slow today?" value={text} onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(text); } }} />
        <div className="row">
          <button type="button" className="btn ghost sm" onClick={() => setShowScope(!showScope)}>{showScope ? <ChevronDown size={14} /> : <ChevronRight size={14} />} Scope & model</button>
          <span className="spacer" />
          <button className="btn primary" disabled={busy || !text.trim()}>{busy ? <Spinner size={14} /> : <Send size={14} />} Ask</button>
        </div>
        {text.trim().length > 5 && (classifyPrompt(text) === "incident"
          ? <div className="intent-hint incident"><Siren size={14} /><span>Sounds like something is broken — this runs as an <b>incident investigation</b>: one timeline across every layer, then the root cause.</span></div>
          : <div className="intent-hint"><MessageSquareText size={14} /><span>Runs as a <b>question</b>: a direct answer from your clusters, servers and monitoring.</span></div>)}
        {showScope && <ScopePicker value={scope} onChange={setScope} />}
        <ErrorBox error={err} />
      </form>
      <div className="form-grid" style={{ marginTop: 22 }}>
        {SUGGESTIONS.map((g) => (
          <div key={g.group} className="panel panel-pad">
            <h3 className="muted small" style={{ textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 8 }}>{g.group}</h3>
            <div className="col" style={{ gap: 6 }}>
              {g.items.map((s) => <button key={s} className="chip" style={{ justifyContent: "flex-start", textAlign: "left", borderRadius: 8, padding: "7px 10px" }} disabled={busy} onClick={() => ask(s)}>{s}</button>)}
            </div>
          </div>
        ))}
      </div>
      {!!recent.data?.items.length && (
        <div style={{ marginTop: 26 }}>
          <h2 style={{ marginBottom: 10 }}>Recent questions</h2>
          <div className="panel">
            {recent.data.items.map((q) => (
              <div key={q.id} className="row clickable" style={{ padding: "10px 14px", borderBottom: "1px solid var(--border)", cursor: "pointer" }} onClick={() => navigate(`/investigations/${q.id}`)}>
                <MessageSquareText size={15} className="muted" /><span className="grow">{q.title}</span><span className="muted small">{timeAgo(q.created_at)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
