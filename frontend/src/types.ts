export type Health = "healthy" | "warning" | "critical" | "unknown";
export type Kind = "llm" | "kubernetes" | "vm" | "monitoring";

export interface Me { name: string; role: "viewer" | "operator" | "admin"; remediation_mode: string; auth_mode: string; version: string }

export interface JsonSchema {
  type?: string; title?: string; description?: string; default?: unknown; enum?: unknown[]; format?: string;
  properties?: Record<string, JsonSchema>; required?: string[]; items?: JsonSchema; anyOf?: JsonSchema[];
  additionalProperties?: boolean | JsonSchema; $ref?: string; $defs?: Record<string, JsonSchema>;
  const?: unknown; minimum?: number; maximum?: number; writeOnly?: boolean; "ui:widget"?: string;
}

export interface PluginInfo {
  kind: Kind; type: string; display_name: string; description: string; capabilities: string[];
  schema: JsonSchema; secret_fields: string[];
}

export interface Integration {
  id: string; name: string; kind: Kind; type: string; description: string; config: Record<string, unknown>;
  enabled: boolean; is_default: boolean; managed_by: string; plugin_available: boolean; updated_at: string | null;
}

export interface ClusterRef { id: string; name: string; type: string; enabled: boolean; environment: string; is_default: boolean }

export interface ResourceSummary {
  id?: string; kind: string; name: string; namespace: string | null; health: Health; status: string;
  reasons: string[]; age: string; labels: Record<string, string>; details: Record<string, unknown>;
}

export interface NamespaceInfo {
  name: string; health: Health; counts: Record<string, number>; age: string;
  issues: { kind: string; name: string; health: Health; reasons: string[] }[];
}

export interface Overview {
  cluster: string; environment: string; health: Health;
  totals: { namespaces: number; nodes: number; pods: number; unhealthy_pods: number; workloads: number };
  namespaces: NamespaceInfo[]; nodes: ResourceSummary[]; errors: Record<string, string>;
}

export interface TopoEdge { id: string; source: string; target: string; relation: string }
export interface Topology { nodes: (ResourceSummary & { id: string })[]; edges: TopoEdge[]; errors: Record<string, string> }

export interface KEvent { type: string; reason: string; object: string; namespace: string; message: string; count: number; age: string }
export interface ResourceDetail { summary: ResourceSummary; events: KEvent[]; yaml: string }

export interface InvEvent { seq: number; type: string; data: any; created_at: string }

export interface Remediation {
  id: string; investigation_id: string; action: string; integration_id: string; params: Record<string, unknown>;
  title: string; rationale: string; risk: "low" | "medium" | "high"; status: string;
  plan: { summary?: string; changes?: string[]; impact?: string; rollback?: string };
  dry_run: { ok: boolean; output?: unknown; error?: string } | null;
  result: { ok?: boolean; output?: unknown; error?: string; reason?: string } | null;
  decided_by: string | null; created_at: string;
}

export interface Report {
  title: string; severity: string; status: string; summary: string; impact: string;
  root_cause: { description: string; category: string; confidence: number; affected_components: string[] } | null;
  evidence: { source: string; finding: string; detail?: string | null }[];
  timeline: { time: string; event: string }[];
  contributing_factors: string[]; other_findings: string[];
  recommendations: { title: string; description: string; kind: string; automated_action: unknown }[];
  open_questions: string[];
}

export interface Investigation {
  id: string; kind: "incident" | "question"; title: string; prompt: string; scope: Record<string, unknown>;
  status: string; severity: string | null; summary: string | null; root_cause: string | null; error: string | null;
  created_by: string; created_at: string; completed_at: string | null; running: boolean;
}

export interface InvestigationDetail extends Investigation { report: Report | null; events: InvEvent[]; remediations: Remediation[] }
