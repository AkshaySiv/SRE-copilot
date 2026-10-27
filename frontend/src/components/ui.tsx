import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Box, Boxes, Cable, CalendarClock, Container, Database, FileCog, Globe, HardDrive, KeyRound, Layers,
  Loader2, Network, Scaling, Server, Workflow,
} from "lucide-react";
import type { Health } from "../types";

export function HealthPill({ health, label }: { health: Health | string; label?: string }) {
  return <span className={`pill h-${health}`}><span className={`dot h-${health}`} />{label ?? health}</span>;
}

export function HealthDot({ health }: { health: Health | string }) {
  return <span className={`dot h-${health}`} title={health} />;
}

export function RiskPill({ risk }: { risk: string }) {
  return <span className={`pill risk-${risk}`}>{risk} risk</span>;
}

const KIND_ICONS: Record<string, typeof Box> = {
  Pod: Container, Deployment: Layers, StatefulSet: Database, DaemonSet: Boxes, ReplicaSet: Layers, Job: Workflow,
  CronJob: CalendarClock, Service: Network, Ingress: Globe, PersistentVolumeClaim: HardDrive, ConfigMap: FileCog,
  Secret: KeyRound, Node: Server, HorizontalPodAutoscaler: Scaling, Namespace: Cable,
};

export function KindIcon({ kind, size = 14 }: { kind: string; size?: number }) {
  const Icon = KIND_ICONS[kind] ?? Box;
  return <Icon size={size} />;
}

export const KIND_SHORT: Record<string, string> = {
  PersistentVolumeClaim: "PVC", HorizontalPodAutoscaler: "HPA",
};

export function Markdown({ children }: { children: string }) {
  return <div className="md"><ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown></div>;
}

export function Spinner({ size = 16 }: { size?: number }) {
  return <Loader2 size={size} className="spin" />;
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  return <div className="error-box">{error instanceof Error ? error.message : String(error)}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

/** Fetch helper with loading/error state and optional polling. */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[], pollMs?: number) {
  const [data, setData] = useState<T | undefined>();
  const [error, setError] = useState<unknown>();
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const reload = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      setData(await fnRef.current());
      setError(undefined);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    setData(undefined);
    reload();
    if (!pollMs) return;
    const t = setInterval(() => reload(true), pollMs);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error, loading, reload, setData };
}

export function Modal({ title, onClose, children, footer }: { title: ReactNode; onClose: () => void; children: ReactNode; footer?: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true">
        <div className="modal-head"><h2 className="grow">{title}</h2><button className="btn ghost sm" onClick={onClose}>Close</button></div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}
