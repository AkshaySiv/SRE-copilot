/**
 * Renders a form from a plugin's pydantic JSON schema, so new integration types need zero UI code.
 * Scalars get native inputs; lists of strings / string maps get line editors; nested objects get a JSON editor.
 */
import { useEffect, useState } from "react";
import type { JsonSchema } from "../types";

const MASK = "••••••••";

function resolve(schema: JsonSchema, root: JsonSchema): JsonSchema {
  if (schema.$ref) {
    const name = schema.$ref.split("/").pop()!;
    return { ...(root.$defs?.[name] ?? {}), description: schema.description ?? root.$defs?.[name]?.description };
  }
  if (schema.anyOf) {
    const nonNull = schema.anyOf.filter((s) => s.type !== "null");
    if (nonNull.length === 1) return { ...resolve(nonNull[0], root), description: schema.description, default: schema.default, title: schema.title, "ui:widget": schema["ui:widget"], format: schema.format ?? nonNull[0].format };
  }
  return schema;
}

type FieldKind = "text" | "password" | "textarea" | "number" | "bool" | "enum" | "list" | "map" | "json";

function fieldKind(s: JsonSchema, secret: boolean): FieldKind {
  if (s.enum) return "enum";
  if (s.type === "boolean") return "bool";
  if (s.type === "integer" || s.type === "number") return "number";
  if (s.type === "array" && s.items && (s.items.type === "string" || !s.items.type && !s.items.$ref)) return "list";
  if (s.type === "object" && typeof s.additionalProperties === "object" && s.additionalProperties.type === "string") return "map";
  if (s.type === "array" || s.type === "object") return "json";
  if (s["ui:widget"] === "textarea") return secret ? "textarea" : "textarea";
  if (secret || s.format === "password") return "password";
  return "text";
}

function toEditable(kind: FieldKind, v: unknown): string {
  if (v === undefined || v === null) return "";
  if (kind === "list") return (v as string[]).join("\n");
  if (kind === "map") return Object.entries(v as Record<string, string>).map(([k, x]) => `${k}=${x}`).join("\n");
  if (kind === "json") return JSON.stringify(v, null, 2);
  return String(v);
}

function fromEditable(kind: FieldKind, raw: string): unknown {
  if (kind === "list") return raw.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);
  if (kind === "map") return Object.fromEntries(raw.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
    const i = l.indexOf("="); return i < 0 ? [l, ""] : [l.slice(0, i).trim(), l.slice(i + 1).trim()];
  }));
  if (kind === "json") return raw.trim() ? JSON.parse(raw) : undefined;
  if (kind === "number") return raw === "" ? undefined : Number(raw);
  return raw === "" ? null : raw;
}

export function initialValues(schema: JsonSchema, existing?: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [name, prop] of Object.entries(schema.properties ?? {})) {
    if (existing && name in existing) out[name] = existing[name];
    else if (prop.default !== undefined) out[name] = prop.default;
  }
  return out;
}

export function SchemaForm({ schema, secretFields, value, onChange, onValidity }: {
  schema: JsonSchema; secretFields: string[]; value: Record<string, unknown>;
  onChange: (v: Record<string, unknown>) => void; onValidity?: (ok: boolean) => void;
}) {
  const props = Object.entries(schema.properties ?? {});
  const required = new Set(schema.required ?? []);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});

  useEffect(() => { setDrafts({}); setErrors({}); }, [schema]);
  useEffect(() => { onValidity?.(Object.keys(errors).length === 0); }, [errors, onValidity]);

  if (!props.length) return <p className="muted">This integration needs no configuration.</p>;

  return (
    <div className="form-grid">
      {props.map(([name, raw]) => {
        const s = resolve(raw, schema);
        const secret = secretFields.includes(name);
        const kind = fieldKind(s, secret);
        const label = (raw.title ?? s.title ?? name).replace(/_/g, " ");
        const full = ["textarea", "list", "map", "json"].includes(kind) || (s.description?.length ?? 0) > 70;
        const isMasked = secret && value[name] === MASK;
        const draft = drafts[name] ?? (isMasked ? "" : toEditable(kind, value[name]));
        const set = (txt: string) => {
          setDrafts((d) => ({ ...d, [name]: txt }));
          try {
            const parsed = fromEditable(kind, txt);
            setErrors(({ [name]: _, ...rest }) => rest);
            onChange({ ...value, [name]: secret && txt === "" && isMasked ? MASK : parsed });
          } catch {
            setErrors((e) => ({ ...e, [name]: "Invalid JSON" }));
          }
        };
        return (
          <div key={name} className={full ? "full" : ""}>
            {kind !== "bool" && <label className="label">{label}{required.has(name) && <span style={{ color: "var(--critical)" }}> *</span>}</label>}
            {kind === "bool" ? (
              <label className="check"><input type="checkbox" checked={Boolean(value[name])} onChange={(e) => onChange({ ...value, [name]: e.target.checked })} /> <span>{label}</span></label>
            ) : kind === "enum" ? (
              <select className="select" value={String(value[name] ?? "")} onChange={(e) => onChange({ ...value, [name]: e.target.value })}>
                {!required.has(name) && <option value="">—</option>}
                {s.enum!.map((o) => <option key={String(o)} value={String(o)}>{String(o)}</option>)}
              </select>
            ) : kind === "textarea" || kind === "list" || kind === "map" || kind === "json" ? (
              <textarea className="textarea mono" rows={kind === "json" ? 7 : 4} value={draft} spellCheck={false}
                placeholder={isMasked ? "Stored securely — leave empty to keep" : kind === "list" ? "one per line" : kind === "map" ? "key=value per line" : kind === "json" ? exampleFor(s, schema) : ""}
                onChange={(e) => set(e.target.value)} />
            ) : (
              <input className="input" type={kind === "password" ? "password" : kind === "number" ? "number" : "text"} value={draft}
                autoComplete="off" placeholder={isMasked ? "Stored securely — leave empty to keep" : ""} onChange={(e) => set(e.target.value)} />
            )}
            {(s.description || errors[name]) && <div className="help" style={errors[name] ? { color: "var(--critical)" } : {}}>{errors[name] ?? s.description}</div>}
          </div>
        );
      })}
    </div>
  );
}

function exampleFor(s: JsonSchema, root: JsonSchema): string {
  const item = s.items ? resolve(s.items, root) : undefined;
  if (item?.properties) {
    const ex = Object.fromEntries(Object.entries(item.properties).slice(0, 4).map(([k, p]) => [k, p.default ?? (p.type === "integer" ? 0 : `<${k}>`)]));
    return JSON.stringify([ex], null, 2);
  }
  return "JSON";
}
