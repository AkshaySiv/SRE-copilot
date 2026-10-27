import { memo, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import dagre from "@dagrejs/dagre";
import {
  Background, Controls, Handle, MiniMap, Position, ReactFlow, ReactFlowProvider, useReactFlow,
  type Edge, type Node, type NodeProps,
} from "@xyflow/react";
import { Filter, RefreshCw, Search, Send, Server, Siren, Sparkles, X } from "lucide-react";
import { api } from "../api";
import { startInvestigation } from "../lib";
import type { ClusterRef, KEvent, Overview, ResourceDetail, ResourceSummary, Topology } from "../types";
import { Empty, ErrorBox, HealthDot, HealthPill, KIND_SHORT, KindIcon, Spinner, useAsync } from "../components/ui";

const NODE_W = 210;
const NODE_H = 66;
type KData = ResourceSummary & { id: string; selected?: boolean; dim?: boolean } & Record<string, unknown>;

const KNode = memo(({ data }: NodeProps<Node<KData>>) => (
  <div className={`knode h-${data.health}${data.selected ? " selected" : ""}`} style={{ opacity: data.dim ? 0.35 : 1 }}>
    <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
    <div className="k"><KindIcon kind={data.kind} size={12} />{KIND_SHORT[data.kind] ?? data.kind}<span className="spacer" /><HealthDot health={data.health} /></div>
    <div className="n" title={data.name}>{data.name}</div>
    <div className="s" title={data.reasons[0] ?? data.status}>{data.reasons[0] ?? data.status}</div>
    <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
  </div>
));
const nodeTypes = { k8s: KNode };

function layout(topo: Topology, selected: string | null, onlyProblems: boolean, query: string) {
  let nodes = topo.nodes;
  let edges = topo.edges;
  if (onlyProblems) {
    const bad = new Set(nodes.filter((n) => n.health === "critical" || n.health === "warning").map((n) => n.id));
    const keep = new Set(bad);
    edges.forEach((e) => { if (bad.has(e.source) || bad.has(e.target)) { keep.add(e.source); keep.add(e.target); } });
    nodes = nodes.filter((n) => keep.has(n.id));
    edges = edges.filter((e) => keep.has(e.source) && keep.has(e.target));
  }
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 18, ranksep: 70, marginx: 20, marginy: 20 });
  g.setDefaultEdgeLabel(() => ({}));
  nodes.forEach((n) => g.setNode(n.id, { width: NODE_W, height: NODE_H }));
  edges.forEach((e) => g.setEdge(e.source, e.target));
  dagre.layout(g);
  const q = query.trim().toLowerCase();
  const rfNodes: Node<KData>[] = nodes.map((n) => {
    const p = g.node(n.id);
    return { id: n.id, type: "k8s", position: { x: p.x - NODE_W / 2, y: p.y - NODE_H / 2 },
      data: { ...n, selected: n.id === selected, dim: !!q && !n.name.toLowerCase().includes(q) } };
  });
  const healthOf = new Map(nodes.map((n) => [n.id, n.health]));
  const rfEdges: Edge[] = edges.map((e) => {
    const bad = healthOf.get(e.target) === "critical";
    return { id: e.id, source: e.source, target: e.target, animated: e.relation === "routes",
      style: { stroke: bad ? "var(--critical)" : "var(--border-2)", strokeWidth: bad ? 1.8 : 1.2 } };
  });
  return { rfNodes, rfEdges };
}

function Graph({ topo, selected, onSelect, onlyProblems, query }: {
  topo: Topology; selected: string | null; onSelect: (n: KData) => void; onlyProblems: boolean; query: string;
}) {
  const { rfNodes, rfEdges } = useMemo(() => layout(topo, selected, onlyProblems, query), [topo, selected, onlyProblems, query]);
  const flow = useReactFlow();
  useEffect(() => { const t = setTimeout(() => flow.fitView({ padding: 0.15, duration: 300 }), 50); return () => clearTimeout(t); },
    [topo, onlyProblems, flow]);
  if (!rfNodes.length) return <Empty>{onlyProblems ? "No problems in this namespace 🎉" : "No resources in this namespace."}</Empty>;
  return (
    <ReactFlow nodes={rfNodes} edges={rfEdges} nodeTypes={nodeTypes} fitView minZoom={0.15} maxZoom={1.8}
      nodesDraggable={false} nodesConnectable={false} onNodeClick={(_, n) => onSelect(n.data as KData)} proOptions={{ hideAttribution: true }}>
      <Background gap={22} size={1} color="var(--border)" />
      <Controls showInteractive={false} />
      <MiniMap pannable zoomable nodeColor={(n) => `var(--${(n.data as KData).health})`} maskColor="rgba(0,0,0,0.4)" />
    </ReactFlow>
  );
}

function ResourceDrawer({ clusterId, node, onClose }: { clusterId: string; node: ResourceSummary; onClose: () => void }) {
  const navigate = useNavigate();
  const [tab, setTab] = useState<"overview" | "events" | "yaml" | "logs">("overview");
  const ns = node.namespace ?? undefined;
  const isRef = node.status === "referenced" || node.status === "missing";
  const detail = useAsync(() => isRef ? Promise.resolve(undefined) :
    api<ResourceDetail>(`/clusters/${clusterId}/resources/${node.kind}/${encodeURIComponent(node.name)}${ns ? `?namespace=${ns}` : ""}`),
    [clusterId, node.kind, node.name, ns]);
  const [busy, setBusy] = useState(false);
  const scope = { clusters: [clusterId], namespace: ns, resource: { kind: node.kind, name: node.name } };
  const ref = `${node.kind} ${ns ? ns + "/" : ""}${node.name}`;
  const go = async (kind: "incident" | "question", prompt: string) => {
    setBusy(true);
    try { await startInvestigation(navigate, { kind, prompt, scope }); } finally { setBusy(false); }
  };
  const containers = (node.details.containers as string[] | undefined) ?? [];

  return (
    <div className="drawer">
      <div className="drawer-head">
        <div className="step-icon"><KindIcon kind={node.kind} /></div>
        <div className="grow">
          <div className="muted small">{node.kind}{ns ? ` · ${ns}` : ""}</div>
          <h2 style={{ wordBreak: "break-all" }}>{node.name}</h2>
          <div className="row" style={{ marginTop: 6 }}><HealthPill health={node.health} /><span className="small text-2">{node.status}</span></div>
        </div>
        <button className="btn ghost sm" onClick={onClose}><X size={16} /></button>
      </div>
      <div className="drawer-body">
        <div className="row wrap" style={{ marginBottom: 14 }}>
          <button className="btn primary" disabled={busy} onClick={() => go("incident", `Investigate ${ref}: ${node.health !== "healthy" ? node.reasons.join("; ") || node.status : "check whether it is working correctly"}`)}>
            <Siren size={15} /> Investigate this
          </button>
          <button className="btn" disabled={busy} onClick={() => go("question", `Explain what ${ref} does, what it depends on, and whether it is healthy.`)}>
            <Sparkles size={15} /> Explain it
          </button>
        </div>
        {node.reasons.length > 0 && (
          <div className="panel panel-pad" style={{ marginBottom: 14, borderColor: `var(--${node.health})` }}>
            <b className="small">Why it is {node.health}</b>
            <ul className="kbd-list small">{node.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
          </div>
        )}
        <div className="tabs">
          {(["overview", "events", "yaml", ...(node.kind === "Pod" ? ["logs"] : [])] as const).map((t) => (
            <button key={t} className={`tab${tab === t ? " active" : ""}`} onClick={() => setTab(t as typeof tab)}>{t}</button>
          ))}
        </div>
        {isRef && <p className="muted">This object is referenced by a pod spec but its contents are not read by the copilot.</p>}
        <ErrorBox error={detail.error} />
        {detail.loading && !isRef && <Spinner />}
        {tab === "overview" && (
          <dl className="kv">
            <dt>Age</dt><dd>{node.age}</dd>
            {Object.entries(node.details).filter(([, v]) => v !== null && v !== undefined && v !== "").map(([k, v]) => (
              <FragmentKV key={k} k={k} v={v} />
            ))}
            {Object.keys(node.labels).length > 0 && <><dt>Labels</dt><dd className="row wrap">{Object.entries(node.labels).map(([k, v]) => <span key={k} className="chip mono">{k}={v}</span>)}</dd></>}
          </dl>
        )}
        {tab === "events" && detail.data && <EventsList events={detail.data.events} />}
        {tab === "yaml" && detail.data && <pre className="code">{detail.data.yaml}</pre>}
        {tab === "logs" && <PodLogs clusterId={clusterId} pod={node.name} namespace={ns!} containers={containers} />}
      </div>
    </div>
  );
}

function FragmentKV({ k, v }: { k: string; v: unknown }) {
  const text = Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : String(v);
  return <><dt>{k}</dt><dd className={typeof v === "object" ? "mono small" : ""}>{text}</dd></>;
}

export function EventsList({ events }: { events: KEvent[] }) {
  if (!events.length) return <p className="muted">No recent events.</p>;
  return (
    <div className="col">
      {events.map((e, i) => (
        <div key={i} className="step" style={{ borderColor: e.type === "Warning" ? "color-mix(in srgb, var(--warning) 40%, transparent)" : undefined }}>
          <div className="grow">
            <div className="row small"><b style={{ color: e.type === "Warning" ? "var(--warning)" : "var(--text-2)" }}>{e.reason}</b>
              <span className="muted">{e.object}</span><span className="spacer" /><span className="muted">×{e.count} · {e.age}</span></div>
            <div className="small text-2">{e.message}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

function PodLogs({ clusterId, pod, namespace, containers }: { clusterId: string; pod: string; namespace: string; containers: string[] }) {
  const [container, setContainer] = useState(containers[0] ?? "");
  const [previous, setPrevious] = useState(false);
  const logs = useAsync(() => api<{ logs: string }>(`/clusters/${clusterId}/logs?pod=${encodeURIComponent(pod)}&namespace=${namespace}&tail=400&previous=${previous}${container ? `&container=${container}` : ""}`),
    [clusterId, pod, namespace, container, previous]);
  return (
    <div className="col">
      <div className="row">
        {containers.length > 1 && <select className="select" style={{ width: 200 }} value={container} onChange={(e) => setContainer(e.target.value)}>{containers.map((c) => <option key={c}>{c}</option>)}</select>}
        <label className="check small"><input type="checkbox" checked={previous} onChange={(e) => setPrevious(e.target.checked)} /> Previous (crashed) instance</label>
        <span className="spacer" /><button className="btn sm" onClick={() => logs.reload()}><RefreshCw size={13} /> Refresh</button>
      </div>
      <ErrorBox error={logs.error} />
      {logs.loading ? <Spinner /> : <pre className="code logs">{logs.data?.logs}</pre>}
    </div>
  );
}

function NodesView({ nodes, onSelect }: { nodes: ResourceSummary[]; onSelect: (n: ResourceSummary) => void }) {
  return (
    <div className="nodes-grid">
      {nodes.map((n) => (
        <button key={n.name} className={`knode h-${n.health}`} style={{ width: "auto" }} onClick={() => onSelect(n)}>
          <div className="k"><Server size={12} /> Node · {(n.details.roles as string[] | undefined)?.join(", ")}<span className="spacer" /><HealthDot health={n.health} /></div>
          <div className="n">{n.name}</div>
          <div className="s">{n.reasons[0] ?? `${n.status} · ${n.details.kubelet ?? ""}`}</div>
          <div className="s muted">{String(n.details.internalIP ?? "")} · {String((n.details.capacity as Record<string, string> | undefined)?.cpu ?? "?")} CPU · {String((n.details.capacity as Record<string, string> | undefined)?.memory ?? "?")}</div>
        </button>
      ))}
    </div>
  );
}

function AskBar({ clusterId, namespace }: { clusterId: string; namespace: string | null }) {
  const navigate = useNavigate();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [focus, setFocus] = useState(false);
  const where = namespace ? `the ${namespace} namespace` : "the cluster";
  const suggestions = [
    `Why is something failing in ${where}?`,
    `Which pods restarted the most in ${where} and why?`,
    `Are any workloads in ${where} close to their memory limits?`,
    `What changed recently in ${where}?`,
  ];
  const send = async (prompt: string) => {
    if (!prompt.trim()) return;
    setBusy(true);
    try { await startInvestigation(navigate, { kind: "question", prompt, scope: { clusters: [clusterId], namespace: namespace ?? undefined } }); }
    finally { setBusy(false); }
  };
  return (
    <div className="ask-bar">
      {focus && !text && (
        <div className="suggestions" style={{ marginBottom: 8, justifyContent: "center" }}>
          {suggestions.map((s) => <button key={s} className="chip" onMouseDown={(e) => { e.preventDefault(); send(s); }}>{s}</button>)}
        </div>
      )}
      <form className="ask-inner" onSubmit={(e) => { e.preventDefault(); send(text); }}>
        <Sparkles size={18} style={{ color: "var(--accent)", alignSelf: "center", marginLeft: 6 }} />
        <input className="input" placeholder={`Ask anything about ${where}…`} value={text} onFocus={() => setFocus(true)}
          onBlur={() => setFocus(false)} onChange={(e) => setText(e.target.value)} />
        <button className="btn primary" disabled={busy || !text.trim()}>{busy ? <Spinner size={14} /> : <Send size={14} />} Ask</button>
      </form>
    </div>
  );
}

function MapInner() {
  const clusters = useAsync(() => api<ClusterRef[]>("/clusters"), []);
  const [clusterId, setClusterId] = useState<string>("");
  const [ns, setNs] = useState<string | null>(null);
  const [selected, setSelected] = useState<ResourceSummary | null>(null);
  const [onlyProblems, setOnlyProblems] = useState(false);
  const [showConfig, setShowConfig] = useState(false);
  const [query, setQuery] = useState("");

  useEffect(() => {
    if (!clusterId && clusters.data?.length) setClusterId((clusters.data.find((c) => c.enabled) ?? clusters.data[0]).id);
  }, [clusters.data, clusterId]);

  const overview = useAsync(() => clusterId ? api<Overview>(`/clusters/${clusterId}/overview`) : Promise.resolve(undefined), [clusterId], 30000);
  useEffect(() => {
    if (overview.data && ns === null) setNs(overview.data.namespaces[0]?.name ?? "__nodes");
  }, [overview.data, ns]);
  const topo = useAsync(() => clusterId && ns && ns !== "__nodes"
    ? api<Topology>(`/clusters/${clusterId}/topology?namespace=${ns}&include_config=${showConfig}`) : Promise.resolve(undefined),
  [clusterId, ns, showConfig], 30000);

  if (clusters.loading) return <div className="page"><Spinner /></div>;
  if (!clusters.data?.length) return (
    <div className="page"><Empty>No Kubernetes clusters configured yet. Add one under <a href="/integrations">Integrations</a> —
      or start the server with <code>SRECOPILOT_SEED_DEMO=true</code> to explore a simulated cluster.</Empty></div>
  );
  const ov = overview.data;

  return (
    <div className="page" style={{ maxWidth: "none" }}>
      <div className="page-head">
        <div>
          <h1>Cluster map</h1>
          <p>Live health of every namespace and how its resources connect. Click anything to inspect or investigate it.</p>
        </div>
        <div className="row wrap">
          <select className="select" style={{ width: 240 }} value={clusterId} onChange={(e) => { setClusterId(e.target.value); setNs(null); setSelected(null); }}>
            {clusters.data.map((c) => <option key={c.id} value={c.id} disabled={!c.enabled}>{c.name}{c.environment ? ` (${c.environment})` : ""}</option>)}
          </select>
          <button className="btn" onClick={() => { overview.reload(); topo.reload(); }}><RefreshCw size={14} /> Refresh</button>
        </div>
      </div>
      <ErrorBox error={overview.error} />
      {ov && (
        <div className="stat-row" style={{ marginBottom: 14 }}>
          <div className="stat"><b><HealthPill health={ov.health} /></b><span>Cluster health</span></div>
          <div className="stat"><b>{ov.totals.nodes}</b><span>Nodes</span></div>
          <div className="stat"><b>{ov.totals.namespaces}</b><span>Namespaces</span></div>
          <div className="stat"><b>{ov.totals.workloads}</b><span>Workloads</span></div>
          <div className="stat"><b>{ov.totals.pods}</b><span>Pods</span></div>
          <div className="stat"><b style={{ color: ov.totals.unhealthy_pods ? "var(--critical)" : undefined }}>{ov.totals.unhealthy_pods}</b><span>Unhealthy pods</span></div>
        </div>
      )}
      <div className="map-layout">
        <div className="panel ns-list">
          <button className={`ns-card${ns === "__nodes" ? " active" : ""}`} onClick={() => { setNs("__nodes"); setSelected(null); }}>
            <div className="row"><Server size={14} /><b className="grow">Nodes</b>{ov && <HealthDot health={ov.nodes.reduce((w, n) => (n.health === "critical" || (n.health === "warning" && w !== "critical") ? n.health : w), "healthy" as string)} />}</div>
            <div className="counts">{ov?.nodes.length ?? "…"} machines</div>
          </button>
          {ov?.namespaces.map((n) => (
            <button key={n.name} className={`ns-card${ns === n.name ? " active" : ""}`} onClick={() => { setNs(n.name); setSelected(null); }}>
              <div className="row"><HealthDot health={n.health} /><b className="grow" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{n.name}</b></div>
              <div className="counts">{n.counts.Pod ?? 0} pods · {(n.counts.Deployment ?? 0) + (n.counts.StatefulSet ?? 0) + (n.counts.DaemonSet ?? 0)} workloads · {n.counts.Service ?? 0} services</div>
              {n.issues.slice(0, 2).map((i, k) => (
                <div key={k} className="issue"><HealthDot health={i.health} /><span><b>{i.name}</b> {i.reasons[0]}</span></div>
              ))}
            </button>
          ))}
        </div>
        <div className="panel graph-wrap">
          {ns === "__nodes" ? (
            ov ? <NodesView nodes={ov.nodes} onSelect={setSelected} /> : <Spinner />
          ) : (
            <>
              <div className="graph-toolbar">
                <b>{ns}</b>
                <div className="row" style={{ background: "var(--bg-2)", border: "1px solid var(--border-2)", borderRadius: 8, padding: "0 8px" }}>
                  <Search size={13} className="muted" />
                  <input className="input" style={{ border: 0, background: "transparent", width: 170, padding: "5px 2px" }} placeholder="Highlight by name" value={query} onChange={(e) => setQuery(e.target.value)} />
                </div>
                <label className="check small"><input type="checkbox" checked={onlyProblems} onChange={(e) => setOnlyProblems(e.target.checked)} /><Filter size={13} /> Problems only</label>
                <label className="check small"><input type="checkbox" checked={showConfig} onChange={(e) => setShowConfig(e.target.checked)} /> Config & secrets</label>
                <span className="spacer" />
                <div className="legend">{(["healthy", "warning", "critical", "unknown"] as const).map((h) => <span key={h} className="row" style={{ gap: 5 }}><HealthDot health={h} />{h}</span>)}</div>
                {topo.loading && <Spinner size={14} />}
              </div>
              <ErrorBox error={topo.error} />
              <div className="graph-canvas">
                {topo.data && <Graph topo={topo.data} selected={selected ? (selected as KData).id ?? null : null} onSelect={setSelected} onlyProblems={onlyProblems} query={query} />}
              </div>
            </>
          )}
          {clusterId && <AskBar clusterId={clusterId} namespace={ns && ns !== "__nodes" ? ns : null} />}
        </div>
      </div>
      {selected && <ResourceDrawer clusterId={clusterId} node={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

export default function ClusterMap() {
  return <ReactFlowProvider><MapInner /></ReactFlowProvider>;
}
