import { createContext, useContext, useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { Activity, MessageSquareText, Moon, Network, PlugZap, ScrollText, Siren, Sun } from "lucide-react";
import { ApiError, api, tokenStore } from "./api";
import type { Me } from "./types";
import ClusterMap from "./pages/ClusterMap";
import Investigations from "./pages/Investigations";
import InvestigationDetail from "./pages/InvestigationDetail";
import Ask from "./pages/Ask";
import Integrations from "./pages/Integrations";
import Audit from "./pages/Audit";

export const MeContext = createContext<Me | null>(null);
export const useMe = () => useContext(MeContext);

function Login({ onDone, error }: { onDone: () => void; error?: string }) {
  const [token, setToken] = useState("");
  return (
    <div className="overlay" style={{ background: "var(--bg)" }}>
      <form className="panel panel-pad col" style={{ width: 380 }} onSubmit={(e) => { e.preventDefault(); tokenStore.set(token.trim()); onDone(); }}>
        <div className="brand"><div className="brand-mark"><Activity size={16} /></div><div>SRE Copilot<small>Sign in</small></div></div>
        <label className="label">Access token</label>
        <input className="input" type="password" value={token} onChange={(e) => setToken(e.target.value)} autoFocus />
        {error && <div className="error-box">{error}</div>}
        <button className="btn primary" type="submit" disabled={!token.trim()}>Continue</button>
      </form>
    </div>
  );
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [authError, setAuthError] = useState<string | undefined>();
  const [needLogin, setNeedLogin] = useState(false);
  const [theme, setTheme] = useState<string>(() => { try { return localStorage.getItem("srecopilot.theme") ?? "dark"; } catch { return "dark"; } });

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("srecopilot.theme", theme); } catch { /* ignore */ }
  }, [theme]);

  const load = () => api<Me>("/me").then((m) => { setMe(m); setNeedLogin(false); }).catch((e) => {
    if (e instanceof ApiError && e.status === 401) { setNeedLogin(true); setAuthError(tokenStore.get() ? "Invalid token" : undefined); }
    else setAuthError(String(e));
  });
  useEffect(() => { load(); }, []);

  if (needLogin) return <Login onDone={load} error={authError} />;
  if (!me) return <div className="empty">{authError ?? "Loading…"}</div>;

  const nav = [
    { to: "/map", label: "Cluster map", icon: Network },
    { to: "/ask", label: "Ask Copilot", icon: MessageSquareText },
    { to: "/investigations", label: "Investigations", icon: Siren },
    { to: "/integrations", label: "Integrations", icon: PlugZap },
    ...(me.role !== "viewer" ? [{ to: "/audit", label: "Audit log", icon: ScrollText }] : []),
  ];

  return (
    <MeContext.Provider value={me}>
      <div className="shell">
        <aside className="sidebar">
          <div className="brand"><div className="brand-mark"><Activity size={16} /></div><div>SRE Copilot<small>Incident intelligence</small></div></div>
          {nav.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
              <Icon size={17} /><span>{label}</span>
            </NavLink>
          ))}
          <div className="sidebar-foot">
            <div>{me.name} · <b>{me.role}</b></div>
            <div>Remediation: {me.remediation_mode}</div>
            <div className="row">
              <button className="btn sm" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>{theme === "dark" ? <Sun size={13} /> : <Moon size={13} />} Theme</button>
              {me.auth_mode === "token" && <button className="btn sm ghost" onClick={() => { tokenStore.set(null); location.reload(); }}>Sign out</button>}
            </div>
            <div>v{me.version}</div>
          </div>
        </aside>
        <main className="main">
          <Routes>
            <Route path="/" element={<Navigate to="/map" replace />} />
            <Route path="/map" element={<ClusterMap />} />
            <Route path="/ask" element={<Ask />} />
            <Route path="/investigations" element={<Investigations />} />
            <Route path="/investigations/:id" element={<InvestigationDetail />} />
            <Route path="/integrations" element={<Integrations />} />
            <Route path="/audit" element={<Audit />} />
            <Route path="*" element={<Navigate to="/map" replace />} />
          </Routes>
        </main>
      </div>
    </MeContext.Provider>
  );
}
