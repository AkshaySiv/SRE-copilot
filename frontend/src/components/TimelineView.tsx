/**
 * Time-based investigation views: causal chain, swimlanes per technology layer, event log and evidence.
 * All four read the same correlated timeline and share one selected event (owned by the page).
 */
import { useMemo, useState } from "react";
import { ArrowRight, Check, Copy, Filter, MessageSquareText } from "lucide-react";
import type { LayerId, Report, TimelineData, TimelineEvent } from "../types";
import { Spinner } from "./ui";

export const LAYER_COLOR: Record<LayerId, string> = {
  change: "#c4b5fd", infra: "#fb923c", k8s: "#60a5fa", dependency: "#f472b6",
  app: "#facc15", network: "#2dd4bf", impact: "#f87171",
};

const ms = (iso: string) => new Date(iso).getTime();
export const hms = (iso: string) => new Date(iso).toISOString().slice(11, 19);
const rgba = (hex: string, a: number) => {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
};
const duration = (s: number) => (s < 60 ? `${s} s` : s < 3600 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${(s / 3600).toFixed(1)} h`);

export interface Derived {
  byId: Map<string, TimelineEvent>;
  chain: string[];
  origin: string | null;
  chainPos: Map<string, number>;
  unrelated: Set<string>;
  confirmed: boolean;
}

/** Prefer the model-reviewed chain from the report when every id it names exists in the timeline. */
export function derive(tl: TimelineData, report: Report | null): Derived {
  const byId = new Map(tl.events.map((e) => [e.id, e]));
  const rChain = (report?.causal_chain ?? []).filter((id) => byId.has(id));
  const confirmed = rChain.length >= 2;
  const chain = confirmed ? rChain : tl.chain;
  const origin = (report?.origin_event_id && byId.has(report.origin_event_id) ? report.origin_event_id : null) ?? chain[0] ?? tl.origin;
  return { byId, chain, origin, chainPos: new Map(chain.map((id, i) => [id, i + 1])), unrelated: new Set(tl.unrelated), confirmed: confirmed || !!report };
}

function roleOf(e: TimelineEvent, d: Derived) {
  if (e.id === d.origin) return { text: "Origin — where the incident started", color: "#fb923c" };
  const n = d.chainPos.get(e.id);
  if (n) return { text: `Causal chain step ${n} of ${d.chain.length}`, color: "var(--text)" };
  if (d.unrelated.has(e.id)) return { text: "Unrelated — not connected to the problem", color: "var(--muted)" };
  return { text: "Supporting evidence", color: "var(--text-2)" };
}

/* ---------------------------------------------------------------- verdict + causal chain */
export function TimelineVerdict({ tl, d, report, running, selected, onSelect }: {
  tl: TimelineData; d: Derived; report: Report | null; running: boolean; selected: string | null; onSelect: (id: string) => void;
}) {
  const origin = d.origin ? d.byId.get(d.origin) : undefined;
  const rc = report?.root_cause;
  const toImpact = tl.stats.origin_to_impact_seconds as number | undefined;
  const focusNames = tl.focus.map((f) => f.split("/").slice(-1)[0]);
  const originIsFocus = origin && tl.focus.some((f) => origin.entity === f || origin.attrs?.group === f);
  return (
    <div className="panel tl-verdict">
      <div className="tl-verdict-top">
        <div style={{ flex: "999 1 420px", minWidth: 0 }}>
          <div className="row small wrap" style={{ gap: 8 }}>
            {report ? <b className={`sev-${report.severity}`} style={{ textTransform: "uppercase", letterSpacing: "0.06em" }}>{report.severity}</b>
              : <span className="pill h-unknown">{running ? "model reviewing…" : "correlated"}</span>}
            <span className="muted">· {report ? report.status.replace(/_/g, " ") : "proposed by timing and dependencies"}</span>
          </div>
          <h2 style={{ margin: "6px 0 4px", fontSize: 18 }}>{report?.title ?? (origin ? `Started at ${origin.title}` : "No clear origin found yet")}</h2>
          <p className="text-2" style={{ margin: 0 }}>
            {report?.summary ?? (origin ? `Earliest connected failure is on the ${tl.layers.find((l) => l.id === origin.layer)?.name} layer. The model is reviewing the chain.` : "Collected evidence did not show a failure connected to the focus.")}
            {origin && !originIsFocus && focusNames.length > 0 && (
              <b style={{ color: "var(--text)" }}> The resource you asked about ({focusNames[0]}) is downstream — the origin is elsewhere.</b>
            )}
          </p>
        </div>
        <div className="tl-verdict-side">
          {rc && (<>
            <div className="row small muted" style={{ justifyContent: "space-between" }}><span>Confidence</span><b style={{ color: "var(--text)" }}>{Math.round(rc.confidence * 100)}%</b></div>
            <div className="confidence"><div style={{ width: `${rc.confidence * 100}%` }} /></div>
          </>)}
          {origin && <div className="row small muted" style={{ justifyContent: "space-between" }}><span>Origin layer</span>
            <span className="layer-badge" style={{ background: rgba(LAYER_COLOR[origin.layer], 0.16), color: LAYER_COLOR[origin.layer] }}>{tl.layers.find((l) => l.id === origin.layer)?.name}</span></div>}
          {toImpact !== undefined && <div className="row small muted" style={{ justifyContent: "space-between" }}><span>Time to user impact</span><b style={{ color: "var(--text)" }}>{duration(toImpact)}</b></div>}
          <div className="row small muted" style={{ justifyContent: "space-between" }}><span>Evidence</span><b style={{ color: "var(--text)" }}>{tl.stats.events} events · {tl.stats.raw_signals} signals</b></div>
        </div>
      </div>
      {d.chain.length > 0 && (
        <div>
          <div className="tl-label">Causal chain {d.confirmed ? "" : "(proposed)"} — click a step</div>
          <div className="row wrap" style={{ gap: 6 }}>
            {d.chain.map((id, i) => {
              const e = d.byId.get(id);
              if (!e) return null;
              const c = LAYER_COLOR[e.layer];
              return (
                <span key={id} className="row" style={{ gap: 6 }}>
                  <button className="chain-chip" onClick={() => onSelect(id)}
                    style={{ borderColor: selected === id ? c : undefined, background: selected === id ? rgba(c, 0.16) : undefined }}>
                    <span className="chain-num" style={{ background: c }}>{i + 1}</span>
                    <span className="col" style={{ gap: 0, alignItems: "flex-start", lineHeight: 1.2, minWidth: 0 }}>
                      <span className="chain-title" title={e.title}>{e.title}</span>
                      <span className="small muted">{tl.layers.find((l) => l.id === e.layer)?.name} · {hms(e.ts)}{e.estimated ? " ~" : ""}</span>
                    </span>
                  </button>
                  {i < d.chain.length - 1 && <ArrowRight size={16} className="muted" aria-hidden="true" />}
                </span>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- swimlanes */
const STEPS = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600].map((s) => s * 1000);

export function Swimlanes({ tl, d, selected, onSelect }: {
  tl: TimelineData; d: Derived; selected: string | null; onSelect: (id: string) => void;
}) {
  const [hidden, setHidden] = useState<Set<LayerId>>(new Set());
  const [chainOnly, setChainOnly] = useState(false);
  const [full, setFull] = useState(false);

  const visible = tl.events.filter((e) => !hidden.has(e.layer) && (!chainOnly || d.chainPos.has(e.id)));
  const { start, end } = useMemo(() => {
    if (full) return { start: ms(tl.window_start), end: ms(tl.window_end) };
    // Start just before the origin (or the first related problem), end after the last related activity.
    const chainEvents = d.chain.map((id) => d.byId.get(id)).filter(Boolean) as TimelineEvent[];
    const related = tl.events.filter((e) => !d.unrelated.has(e.id) && e.severity !== "info");
    const anchorPool = chainEvents.length ? chainEvents : related.length ? related : tl.events;
    if (!anchorPool.length) return { start: ms(tl.window_start), end: ms(tl.window_end) };
    const s = Math.min(...anchorPool.map((e) => ms(e.ts)));
    const tailPool = (related.length ? related : tl.events).filter((x) => ms(x.ts) >= s);
    const e = Math.max(...anchorPool.map((x) => ms(x.ts)), ...tailPool.map((x) => ms(x.last_ts ?? x.ts)));
    const pad = Math.max((e - s) * 0.06, 15000);
    return { start: s - pad, end: e + pad };
  }, [tl, d, full]);
  const span = Math.max(end - start, 1000);
  const pct = (iso: string) => ((ms(iso) - start) / span) * 100;
  const step = STEPS.find((s) => span / s <= 9) ?? 7200000;
  const ticks: number[] = [];
  for (let t = Math.ceil(start / step) * step; t <= end; t += step) ticks.push(t);
  const outside = visible.filter((e) => pct(e.ts) < 0 || pct(e.ts) > 100).length;
  const sel = selected ? d.byId.get(selected) : undefined;

  const lanes = tl.layers.filter((l) => !hidden.has(l.id)).map((l) => {
    const evs = visible.filter((e) => e.layer === l.id && pct(e.ts) >= 0 && pct(e.ts) <= 100).sort((a, b) => ms(a.ts) - ms(b.ts));
    let lastPct = -10; let flip = 0;
    const placed = evs.map((e) => {
      const p = pct(e.ts);
      flip = p - lastPct < 1.6 ? flip + 1 : 0;
      lastPct = p;
      return { e, p, dy: flip === 0 ? 0 : (flip % 2 ? -13 : 13) };
    });
    return { ...l, placed };
  });

  const toggle = (id: LayerId) => setHidden((h) => { const n = new Set(h); n.has(id) ? n.delete(id) : n.add(id); return n; });

  return (
    <div className="panel" style={{ overflow: "hidden" }}>
      <div className="tl-toolbar">
        <div className="grow" style={{ minWidth: 200 }}>
          <h2 style={{ fontSize: 16 }}>Timeline across layers</h2>
          <div className="small muted">{visible.length} events · time runs left → right (UTC){outside ? ` · ${outside} outside this view` : ""}</div>
        </div>
        <div className="row wrap" style={{ gap: 6 }}>
          {tl.layers.map((l) => {
            const on = !hidden.has(l.id);
            return (
              <button key={l.id} className="layer-chip" aria-pressed={on} onClick={() => toggle(l.id)}
                style={{ borderColor: on ? rgba(LAYER_COLOR[l.id], 0.5) : undefined, background: on ? rgba(LAYER_COLOR[l.id], 0.12) : "transparent", color: on ? "var(--text)" : "var(--muted)" }}>
                <span className="dot" style={{ background: on ? LAYER_COLOR[l.id] : "var(--unknown)", boxShadow: "none" }} />{l.name}
              </button>
            );
          })}
          <button className="layer-chip" aria-pressed={chainOnly} onClick={() => setChainOnly(!chainOnly)}
            style={chainOnly ? { borderColor: "var(--accent)", background: "color-mix(in srgb, var(--accent) 16%, transparent)" } : {}}>
            <Filter size={12} /> Causal chain only</button>
          <button className="layer-chip" aria-pressed={full} onClick={() => setFull(!full)}>{full ? "Focus on incident" : "Full window"}</button>
        </div>
      </div>
      <div className="tl-scroll">
        <div className="tl-grid">
          <div className="tl-row tl-axis">
            <div className="tl-lane-label" />
            <div className="tl-track">
              {ticks.map((t) => <span key={t} className="tl-tick-label" style={{ left: `${((t - start) / span) * 100}%` }}>{new Date(t).toISOString().slice(11, step < 60000 ? 19 : 16)}</span>)}
            </div>
          </div>
          {lanes.map((l) => (
            <div key={l.id} className="tl-row" style={{ background: sel?.layer === l.id ? "color-mix(in srgb, var(--accent) 4%, transparent)" : undefined }}>
              <div className="tl-lane-label">
                <span className="tl-swatch" style={{ background: LAYER_COLOR[l.id] }} />
                <div style={{ minWidth: 0 }}><div className="tl-lane-name">{l.name}</div><div className="tl-lane-stack">{l.stack}</div></div>
              </div>
              <div className="tl-track">
                {ticks.map((t) => <span key={t} className="tl-gridline" style={{ left: `${((t - start) / span) * 100}%` }} />)}
                {sel && pct(sel.ts) >= 0 && pct(sel.ts) <= 100 && <span className="tl-cursor" style={{ left: `${pct(sel.ts)}%` }} />}
                {l.placed.length === 0 && <span className="tl-empty">{l.id === "change" ? "No deployments or config changes in view — ruled out" : "No events in view"}</span>}
                {l.placed.map(({ e, p, dy }) => {
                  const n = d.chainPos.get(e.id);
                  const isOrigin = e.id === d.origin;
                  const strong = !!n || isOrigin;
                  const size = isOrigin ? 26 : n ? 22 : 14;
                  const c = LAYER_COLOR[e.layer];
                  const unrelated = d.unrelated.has(e.id);
                  return (
                    <span key={e.id} className="tl-anchor" style={{ left: `${p}%`, top: `calc(50% + ${dy}px)` }}>
                      {isOrigin && <span className="tl-origin-tag">ORIGIN</span>}
                      {e.count > 1 && <span className="tl-count" style={{ color: c }}>×{e.count}</span>}
                      <button className="tl-marker" onClick={() => onSelect(e.id)}
                        aria-label={`${hms(e.ts)} ${l.name}: ${e.title}`} title={`${hms(e.ts)}${e.estimated ? " (estimated)" : ""} · ${e.title}`}
                        style={{
                          width: size, height: size, borderColor: c, background: strong ? c : "var(--panel)",
                          opacity: unrelated ? 0.45 : 1, borderStyle: e.estimated && !strong ? "dashed" : "solid",
                          boxShadow: selected === e.id ? "0 0 0 3px var(--bg), 0 0 0 5px var(--text)" : isOrigin ? `0 0 0 5px ${rgba(c, 0.25)}` : "none",
                        }}>{n ?? ""}</button>
                    </span>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      </div>
      <div className="tl-legend">
        <span><span className="tl-leg" style={{ width: 16, height: 16, background: "#fb923c", boxShadow: "0 0 0 4px rgba(251,146,60,.22)" }} />Origin</span>
        <span><span className="tl-leg" style={{ width: 14, height: 14, background: "#60a5fa" }} />Causal chain step</span>
        <span><span className="tl-leg" style={{ width: 12, height: 12, border: "2px solid #60a5fa" }} />Supporting evidence</span>
        <span><span className="tl-leg" style={{ width: 12, height: 12, border: "2px dashed #60a5fa" }} />Estimated time</span>
        <span><span className="tl-leg" style={{ width: 12, height: 12, border: "2px solid #60a5fa", opacity: 0.45 }} />Unrelated</span>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- event log */
export function textTimeline(tl: TimelineData, d: Derived): string {
  const name = (l: LayerId) => (tl.layers.find((x) => x.id === l)?.name ?? l).toUpperCase().padEnd(11);
  const rows = tl.events.filter((e) => !d.unrelated.has(e.id) && (e.severity !== "info" || d.chainPos.has(e.id) || e.layer === "change"));
  const lines = rows.map((e) => `${hms(e.ts)}${e.estimated ? "~" : " "} ${name(e.layer)} ${e.title}${e.count > 1 ? ` (×${e.count})` : ""}${e.id === d.origin ? "   ◀ ORIGIN" : ""}`);
  const chain = d.chain.map((id) => d.byId.get(id)?.title).filter(Boolean).join(" → ");
  const unrelated = tl.events.filter((e) => d.unrelated.has(e.id)).map((e) => `${hms(e.ts)} ${e.title}`);
  return [...lines, "", `CAUSAL CHAIN  ${chain}`, ...(unrelated.length ? ["UNRELATED", ...unrelated.map((u) => `  ${u}`)] : [])].join("\n");
}

export function EventLog({ tl, d, selected, onSelect }: {
  tl: TimelineData; d: Derived; selected: string | null; onSelect: (id: string) => void;
}) {
  const [copied, setCopied] = useState(false);
  const [showInfo, setShowInfo] = useState(false);
  const related = tl.events.filter((e) => !d.unrelated.has(e.id) && (showInfo || e.severity !== "info" || d.chainPos.has(e.id) || e.layer === "change"));
  const unrelated = tl.events.filter((e) => d.unrelated.has(e.id));
  const copy = async () => {
    try { await navigator.clipboard.writeText(textTimeline(tl, d)); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { /* clipboard blocked */ }
  };
  const Row = ({ e }: { e: TimelineEvent }) => {
    const c = LAYER_COLOR[e.layer];
    const n = d.chainPos.get(e.id);
    return (
      <button className="log-row" onClick={() => onSelect(e.id)} style={{ background: selected === e.id ? "var(--panel-2)" : undefined }}>
        <span className="log-time">{hms(e.ts)}{e.estimated ? "~" : ""}</span>
        <span className="log-layer"><span className="layer-badge" style={{ background: rgba(c, 0.15), color: c }}>{tl.layers.find((l) => l.id === e.layer)?.name}</span></span>
        <span className="log-title">{e.title}{e.count > 1 && <span className="muted"> ×{e.count}</span>}</span>
        {e.id === d.origin ? <span className="origin-tag">◀ ORIGIN</span> : n ? <span className="chain-num" style={{ background: c }}>{n}</span> : null}
      </button>
    );
  };
  return (
    <div className="panel" style={{ overflow: "hidden" }}>
      <div className="tl-toolbar">
        <div className="grow"><h2 style={{ fontSize: 16 }}>Event log</h2><div className="small muted">Oldest first · ~ marks an estimated time</div></div>
        <label className="check small"><input type="checkbox" checked={showInfo} onChange={(e) => setShowInfo(e.target.checked)} /> Show info events</label>
        <button className="btn sm" onClick={copy}>{copied ? <Check size={13} /> : <Copy size={13} />} {copied ? "Copied" : "Copy as text"}</button>
      </div>
      <div>{related.map((e) => <Row key={e.id} e={e} />)}</div>
      {unrelated.length > 0 && (
        <div className="log-unrelated">
          <div className="tl-label">Unrelated — happened nearby, not connected</div>
          {unrelated.map((e) => <Row key={e.id} e={e} />)}
        </div>
      )}
      {tl.errors.length > 0 && (
        <div className="log-unrelated small muted">
          <div className="tl-label">Collection gaps</div>
          {tl.errors.map((er, i) => <div key={i}>{er}</div>)}
        </div>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- evidence */
export function EvidencePanel({ tl, d, selected, onAsk }: {
  tl: TimelineData; d: Derived; selected: string | null; onAsk: (text: string) => void;
}) {
  const e = selected ? d.byId.get(selected) : undefined;
  if (!e) return <div className="panel panel-pad muted">Select an event on the timeline to see its evidence.</div>;
  const c = LAYER_COLOR[e.layer];
  const layer = tl.layers.find((l) => l.id === e.layer);
  const role = roleOf(e, d);
  return (
    <div className="panel panel-pad col" style={{ gap: 12 }}>
      <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
        <span className="layer-badge" style={{ background: rgba(c, 0.15), color: c }}>{layer?.name} · {layer?.stack}</span>
        <span className="mono small text-2">{hms(e.ts)} UTC{e.estimated ? " (estimated)" : ""}</span>
      </div>
      <h3 style={{ fontSize: 15, overflowWrap: "anywhere" }}>{e.title}</h3>
      <dl className="kv small">
        <dt>Role</dt><dd style={{ color: role.color, fontWeight: 600 }}>{role.text}</dd>
        <dt>Resource</dt><dd className="mono">{e.entity}</dd>
        <dt>Source</dt><dd className="text-2">{e.source}</dd>
        {e.count > 1 && <><dt>Occurrences</dt><dd>{e.count}{e.last_ts ? `, last at ${hms(e.last_ts)}` : ""}</dd></>}
        {e.attrs?.pods?.length > 1 && <><dt>Pods</dt><dd className="mono">{e.attrs.pods.join(", ")}</dd></>}
      </dl>
      <div className="tl-label">Evidence</div>
      <pre className="code logs" style={{ maxHeight: 320 }}>{e.evidence || "—"}</pre>
      <div className="row wrap">
        <button className="btn sm" onClick={() => onAsk(`About the event at ${hms(e.ts)} (${e.title}): `)}><MessageSquareText size={13} /> Ask about this event</button>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- progress */
export function PhaseStrip({ phases }: { phases: { name: string; detail: string; state: "done" | "active" | "pending" }[] }) {
  return (
    <div className="phase-strip">
      {phases.map((p) => (
        <div key={p.name} className={`phase ${p.state}`}>
          <span className="phase-icon">{p.state === "done" ? <Check size={14} /> : p.state === "active" ? <Spinner size={13} /> : <span className="dot h-unknown" style={{ boxShadow: "none" }} />}</span>
          <div style={{ minWidth: 0 }}><div className="small" style={{ fontWeight: 600 }}>{p.name}</div><div className="small muted phase-detail">{p.detail}</div></div>
        </div>
      ))}
    </div>
  );
}
