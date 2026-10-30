import { useState } from "react";
import { Lightbulb, Sparkles, Wrench } from "lucide-react";
import { api } from "../api";
import type { AIExplanation, Diagnosis, Insight } from "../types";
import { ErrorBox, Spinner, useAsync } from "./ui";

/** Renders `like this` as inline code - insights quote exact names (secrets, images, env vars). */
function Ticks({ text }: { text: string }) {
  return <>{text.split(/(`[^`]+`)/g).map((p, i) => p.startsWith("`") && p.endsWith("`") ? <code key={i}>{p.slice(1, -1)}</code> : <span key={i}>{p}</span>)}</>;
}

function InsightRow({ i }: { i: Insight }) {
  return (
    <div className="insight">
      <div className="insight-title"><Ticks text={i.title} /></div>
      <div className="row wrap small" style={{ gap: 6, marginTop: 4 }}>
        <span className={`chip conf-${i.confidence}`}>{i.confidence === "high" ? "exact" : "likely"}</span>
        <span className="chip">{i.category}</span>
        {i.subject && <span className="muted">{i.subject}</span>}
      </div>
      {i.fix && <div className="insight-fix small"><Wrench size={12} /><Ticks text={i.fix} /></div>}
      {i.evidence.filter(Boolean).length > 0 && (
        <details className="small" style={{ marginTop: 4 }}>
          <summary className="muted" style={{ cursor: "pointer" }}>evidence</summary>
          <pre className="code" style={{ marginTop: 4, maxHeight: 140, whiteSpace: "pre-wrap" }}>{i.evidence.filter(Boolean).join("\n")}</pre>
        </details>
      )}
    </div>
  );
}

/** "Why it is critical": exact rule-based causes first, an on-demand AI one-liner when the rules can't tell. */
export function WhyPanel({ clusterId, kind, name, namespace, health, reasons }: {
  clusterId: string; kind: string; name: string; namespace?: string; health: string; reasons: string[];
}) {
  const qs = namespace ? `?namespace=${encodeURIComponent(namespace)}` : "";
  const base = `/clusters/${clusterId}/resources/${kind}/${encodeURIComponent(name)}`;
  const diag = useAsync(() => api<Diagnosis>(`${base}/diagnosis${qs}`), [base, qs]);
  const [ai, setAi] = useState<AIExplanation | null>(null);
  const [aiBusy, setAiBusy] = useState(false);
  const [aiErr, setAiErr] = useState<unknown>();
  const insights = diag.data?.insights ?? [];
  if (health === "healthy" && !insights.length) return null;

  const explain = async () => {
    setAiBusy(true); setAiErr(undefined);
    try { setAi(await api<AIExplanation>(`${base}/explain${qs}`, { body: {} })); } catch (e) { setAiErr(e); } finally { setAiBusy(false); }
  };
  const unsure = !insights.length || insights.every((i) => i.confidence !== "high");
  const color = health === "healthy" ? "warning" : health;

  return (
    <div className="panel panel-pad why-panel" style={{ marginBottom: 14, borderColor: `var(--${color})` }}>
      <div className="row"><Lightbulb size={15} style={{ color: `var(--${color})` }} /><b className="small grow">Why it is {health === "healthy" ? "flagged" : health}</b>
        {diag.loading && <Spinner size={13} />}</div>
      {insights.map((i, n) => <InsightRow key={n} i={i} />)}
      {!diag.loading && !insights.length && <ul className="kbd-list small" style={{ marginTop: 6 }}>{reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>}
      {ai && (
        <div className="insight ai">
          <div className="row small muted" style={{ gap: 6 }}><Sparkles size={12} /> AI explanation · {ai.model}{ai.cached ? " · cached" : ""}<span className={`chip conf-${ai.confidence === "high" ? "high" : "medium"}`}>{ai.confidence}</span></div>
          <div className="insight-title" style={{ marginTop: 4 }}><Ticks text={ai.cause} /></div>
          {ai.fix && <div className="insight-fix small"><Wrench size={12} /><Ticks text={ai.fix} /></div>}
        </div>
      )}
      <ErrorBox error={aiErr} />
      {diag.data && !ai && (diag.data.ai_available
        ? <button className={`btn sm ${unsure ? "" : "ghost"}`} style={{ marginTop: 8 }} disabled={aiBusy} onClick={explain}>
            {aiBusy ? <Spinner size={12} /> : <Sparkles size={13} />} {unsure ? "Explain with AI" : "Ask AI for a second opinion"}
          </button>
        : unsure && <div className="muted small" style={{ marginTop: 8 }}>Connect an LLM under Integrations for an AI explanation.</div>)}
      {insights.length > 0 && reasons.length > 0 && (
        <details className="small" style={{ marginTop: 8 }}>
          <summary className="muted" style={{ cursor: "pointer" }}>raw status</summary>
          <ul className="kbd-list">{reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
        </details>
      )}
    </div>
  );
}
