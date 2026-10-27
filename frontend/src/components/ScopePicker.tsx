import { useEffect } from "react";
import { api } from "../api";
import type { Integration } from "../types";
import { useAsync } from "./ui";

export interface Scope { llm?: string; clusters?: string[]; monitoring?: string[]; vms?: string[]; namespace?: string }

function MultiPick({ label, items, value, onChange }: { label: string; items: Integration[]; value?: string[]; onChange: (v?: string[]) => void }) {
  if (!items.length) return null;
  const all = !value || value.length === 0;
  return (
    <div>
      <span className="label">{label}</span>
      <div className="row wrap">
        <button type="button" className="chip" style={all ? { borderColor: "var(--accent)", color: "var(--text)" } : {}} onClick={() => onChange(undefined)}>All</button>
        {items.map((i) => {
          const on = !all && value!.includes(i.id);
          return (
            <button type="button" key={i.id} className="chip" style={on ? { borderColor: "var(--accent)", color: "var(--text)" } : {}}
              onClick={() => onChange(on ? value!.filter((x) => x !== i.id) : [...(value ?? []), i.id])}>{i.name}</button>
          );
        })}
      </div>
    </div>
  );
}

export function ScopePicker({ value, onChange }: { value: Scope; onChange: (s: Scope) => void }) {
  const ints = useAsync(() => api<Integration[]>("/integrations"), []);
  const list = (ints.data ?? []).filter((i) => i.enabled);
  const llms = list.filter((i) => i.kind === "llm");
  useEffect(() => {
    if (!value.llm && llms.length) onChange({ ...value, llm: (llms.find((l) => l.is_default) ?? llms[0]).id });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ints.data]);
  if (ints.loading) return null;
  return (
    <div className="form-grid">
      <div>
        <span className="label">AI model</span>
        {llms.length ? (
          <select className="select" value={value.llm ?? ""} onChange={(e) => onChange({ ...value, llm: e.target.value })}>
            {llms.map((l) => <option key={l.id} value={l.id}>{l.name} ({l.type})</option>)}
          </select>
        ) : <div className="error-box small">No LLM configured — add one under Integrations.</div>}
      </div>
      <div>
        <span className="label">Namespace (optional)</span>
        <input className="input" placeholder="e.g. payments" value={value.namespace ?? ""} onChange={(e) => onChange({ ...value, namespace: e.target.value || undefined })} />
      </div>
      <div className="full"><MultiPick label="Kubernetes clusters" items={list.filter((i) => i.kind === "kubernetes")} value={value.clusters} onChange={(v) => onChange({ ...value, clusters: v })} /></div>
      <div className="full"><MultiPick label="Monitoring & logs" items={list.filter((i) => i.kind === "monitoring")} value={value.monitoring} onChange={(v) => onChange({ ...value, monitoring: v })} /></div>
      <div className="full"><MultiPick label="Virtual machines" items={list.filter((i) => i.kind === "vm")} value={value.vms} onChange={(v) => onChange({ ...value, vms: v })} /></div>
    </div>
  );
}
