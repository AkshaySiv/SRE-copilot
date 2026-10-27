import { api } from "../api";
import { Empty, ErrorBox, Spinner, useAsync } from "../components/ui";

interface Entry { id: number; actor: string; action: string; target: string; details: Record<string, unknown>; created_at: string }

export default function Audit() {
  const log = useAsync(() => api<Entry[]>("/audit?limit=300"), [], 15000);
  return (
    <div className="page">
      <div className="page-head"><div><h1>Audit log</h1><p>Every configuration change, investigation and remediation decision.</p></div></div>
      <ErrorBox error={log.error} />
      <div className="panel">
        {log.loading && !log.data ? <div className="panel-pad"><Spinner /></div> : !log.data?.length ? <Empty>No entries yet.</Empty> : (
          <table className="table">
            <thead><tr><th>When</th><th>Who</th><th>Event</th><th>Target</th><th>Details</th></tr></thead>
            <tbody>
              {log.data.map((e) => (
                <tr key={e.id}>
                  <td className="small muted" style={{ whiteSpace: "nowrap" }}>{new Date(e.created_at).toLocaleString()}</td>
                  <td>{e.actor}</td>
                  <td><span className="chip mono">{e.action}</span></td>
                  <td className="mono small">{e.target}</td>
                  <td className="mono small text-2" style={{ maxWidth: 520, wordBreak: "break-word" }}>{JSON.stringify(e.details)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
