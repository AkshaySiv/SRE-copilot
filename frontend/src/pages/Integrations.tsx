import { useCallback, useState } from "react";
import { BrainCircuit, CheckCircle2, Gauge, Pencil, Plus, Server, Ship, Trash2, XCircle, Zap } from "lucide-react";
import { api } from "../api";
import { useMe } from "../App";
import type { Integration, Kind, PluginInfo } from "../types";
import { SchemaForm, initialValues } from "../components/SchemaForm";
import { Empty, ErrorBox, Modal, Spinner, useAsync } from "../components/ui";

const KINDS: { kind: Kind; title: string; blurb: string; icon: typeof Server }[] = [
  { kind: "llm", title: "AI models", blurb: "The model that reasons over the evidence. Self-hosted endpoints keep everything air-gapped.", icon: BrainCircuit },
  { kind: "kubernetes", title: "Kubernetes clusters", blurb: "Clusters the copilot can read and, if you allow it, remediate.", icon: Ship },
  { kind: "monitoring", title: "Monitoring & logs", blurb: "Prometheus-compatible metrics and alerts, Loki or Elasticsearch logs.", icon: Gauge },
  { kind: "vm", title: "Virtual machines", blurb: "Linux servers over SSH, limited to a catalogue of safe diagnostics.", icon: Server },
];

type TestResult = { ok: boolean; message: string; details?: Record<string, unknown> };

function Editor({ plugins, existing, kind, onClose, onSaved }: {
  plugins: PluginInfo[]; existing?: Integration; kind: Kind; onClose: () => void; onSaved: () => void;
}) {
  const options = plugins.filter((p) => p.kind === kind);
  const [type, setType] = useState(existing?.type ?? (options.length === 1 ? options[0].type : ""));
  const plugin = options.find((p) => p.type === type);
  const [name, setName] = useState(existing?.name ?? "");
  const [description, setDescription] = useState(existing?.description ?? "");
  const [isDefault, setIsDefault] = useState(existing?.is_default ?? false);
  const [enabled, setEnabled] = useState(existing?.enabled ?? true);
  const [config, setConfig] = useState<Record<string, unknown>>(() => plugin ? initialValues(plugin.schema, existing?.config) : {});
  const [valid, setValid] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<unknown>();
  const [test, setTest] = useState<TestResult | null>(null);
  const onValidity = useCallback((ok: boolean) => setValid(ok), []);

  const pick = (t: string) => {
    setType(t);
    const p = options.find((o) => o.type === t)!;
    setConfig(initialValues(p.schema, existing?.type === t ? existing.config : undefined));
    setTest(null);
  };
  const doTest = async () => {
    setBusy("test"); setTest(null); setErr(undefined);
    try { setTest(await api<TestResult>("/integrations/test", { body: { kind, type, config, integration_id: existing?.id } })); }
    catch (e) { setErr(e); } finally { setBusy(null); }
  };
  const save = async () => {
    setBusy("save"); setErr(undefined);
    const body = { name, kind, type, description, config, enabled, is_default: isDefault };
    try {
      if (existing) await api(`/integrations/${existing.id}`, { method: "PUT", body });
      else await api("/integrations", { body });
      onSaved();
    } catch (e) { setErr(e); } finally { setBusy(null); }
  };

  return (
    <Modal title={existing ? `Edit ${existing.name}` : `Add ${KINDS.find((k) => k.kind === kind)?.title.toLowerCase()}`} onClose={onClose}
      footer={plugin && <>
        {test && <span className="row small grow" style={{ color: test.ok ? "var(--healthy)" : "var(--critical)" }}>{test.ok ? <CheckCircle2 size={15} /> : <XCircle size={15} />}{test.message}</span>}
        <button className="btn" disabled={!!busy || !valid} onClick={doTest}>{busy === "test" ? <Spinner size={14} /> : <Zap size={14} />} Test connection</button>
        <button className="btn primary" disabled={!!busy || !valid || name.trim().length < 2} onClick={save}>{busy === "save" && <Spinner size={14} />} Save</button>
      </>}>
      {!existing && options.length > 1 && (
        <div className="type-grid" style={{ marginBottom: 18 }}>
          {options.map((o) => (
            <button key={o.type} className={`type-card${type === o.type ? " active" : ""}`} onClick={() => pick(o.type)}>
              <b>{o.display_name}</b><div className="small muted" style={{ marginTop: 4 }}>{o.description}</div>
            </button>
          ))}
        </div>
      )}
      {plugin ? (
        <div className="col" style={{ gap: 16 }}>
          <div className="form-grid">
            <div><label className="label">Name *</label><input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder={`e.g. ${kind === "kubernetes" ? "prod-rke2" : kind === "llm" ? "vllm-qwen" : kind === "vm" ? "db-servers" : "prometheus-prod"}`} /></div>
            <div><label className="label">Description</label><input className="input" value={description} onChange={(e) => setDescription(e.target.value)} /></div>
            <label className="check"><input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> Enabled</label>
            <label className="check"><input type="checkbox" checked={isDefault} onChange={(e) => setIsDefault(e.target.checked)} /> Default {kind === "llm" ? "model" : "for its kind"}</label>
          </div>
          <div style={{ borderTop: "1px solid var(--border)", paddingTop: 14 }}>
            <SchemaForm schema={plugin.schema} secretFields={plugin.secret_fields} value={config} onChange={setConfig} onValidity={onValidity} />
          </div>
          {existing?.managed_by === "bootstrap" && <div className="muted small">Managed by the bootstrap file — changes here are overwritten on restart.</div>}
          <ErrorBox error={err} />
        </div>
      ) : <p className="muted">Choose a type above.</p>}
    </Modal>
  );
}

function IntegrationCard({ i, plugin, canEdit, onEdit, onChanged }: { i: Integration; plugin?: PluginInfo; canEdit: boolean; onEdit: () => void; onChanged: () => void }) {
  const [test, setTest] = useState<TestResult | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try { setTest(await api<TestResult>(`/integrations/${i.id}/test`, { body: {} })); }
    catch (e) { setTest({ ok: false, message: String(e) }); } finally { setBusy(false); }
  };
  const remove = async () => {
    if (!window.confirm(`Delete integration "${i.name}"?`)) return;
    await api(`/integrations/${i.id}`, { method: "DELETE" });
    onChanged();
  };
  return (
    <div className="panel int-card" style={{ opacity: i.enabled ? 1 : 0.6 }}>
      <div className="row">
        <b className="grow">{i.name}</b>
        {i.is_default && <span className="chip">default</span>}
        {!i.enabled && <span className="chip">disabled</span>}
      </div>
      <div className="small muted">{plugin?.display_name ?? `${i.type} (plugin not installed)`}{i.description ? ` · ${i.description}` : ""}</div>
      {plugin?.capabilities.length ? <div className="row wrap">{plugin.capabilities.map((c) => <span key={c} className="chip">{c}</span>)}</div> : null}
      {test && <div className="small row" style={{ color: test.ok ? "var(--healthy)" : "var(--critical)", alignItems: "flex-start" }}>{test.ok ? <CheckCircle2 size={14} /> : <XCircle size={14} />}<span>{test.message}</span></div>}
      <div className="row" style={{ marginTop: "auto" }}>
        <button className="btn sm" disabled={busy} onClick={run}>{busy ? <Spinner size={12} /> : <Zap size={13} />} Test</button>
        {canEdit && <button className="btn sm" onClick={onEdit}><Pencil size={13} /> Edit</button>}
        <span className="spacer" />
        {canEdit && <button className="btn sm ghost danger" onClick={remove}><Trash2 size={13} /></button>}
      </div>
    </div>
  );
}

export default function Integrations() {
  const me = useMe();
  const plugins = useAsync(() => api<PluginInfo[]>("/plugins"), []);
  const list = useAsync(() => api<Integration[]>("/integrations"), []);
  const [editing, setEditing] = useState<{ kind: Kind; existing?: Integration } | null>(null);
  const canEdit = me?.role === "admin";

  if (plugins.loading || list.loading) return <div className="page"><Spinner /></div>;
  return (
    <div className="page">
      <div className="page-head"><div><h1>Integrations</h1><p>Connect the AI model, clusters, servers and observability tools the copilot uses. Secrets are encrypted at rest and never shown again.</p></div></div>
      <ErrorBox error={plugins.error || list.error} />
      <div className="col" style={{ gap: 26 }}>
        {KINDS.map(({ kind, title, blurb, icon: Icon }) => {
          const items = (list.data ?? []).filter((i) => i.kind === kind);
          return (
            <section key={kind}>
              <div className="row" style={{ marginBottom: 10 }}>
                <Icon size={18} style={{ color: "var(--accent)" }} />
                <div className="grow"><h2>{title}</h2><div className="small muted">{blurb}</div></div>
                {canEdit && <button className="btn" onClick={() => setEditing({ kind })}><Plus size={14} /> Add</button>}
              </div>
              {items.length ? (
                <div className="int-grid">
                  {items.map((i) => <IntegrationCard key={i.id} i={i} plugin={plugins.data?.find((p) => p.kind === i.kind && p.type === i.type)} canEdit={canEdit}
                    onEdit={() => setEditing({ kind, existing: i })} onChanged={() => list.reload(true)} />)}
                </div>
              ) : <div className="panel"><Empty>None configured.</Empty></div>}
            </section>
          );
        })}
      </div>
      {editing && plugins.data && (
        <Editor plugins={plugins.data} kind={editing.kind} existing={editing.existing} onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); list.reload(true); }} />
      )}
    </div>
  );
}
