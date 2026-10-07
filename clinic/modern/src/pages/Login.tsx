import { useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { api, ApiError } from "../api";
import { useApp } from "../context";
import { Problems } from "../components/Modal";

export function Login() {
  const { user, setUser, t } = useApp();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [problems, setProblems] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  if (user) return <Navigate to="/patients" replace />;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setProblems([]);
    try {
      setUser((await api.login(username, password)).user);
      navigate("/patients", { replace: true });
    } catch (err) {
      setProblems([err instanceof ApiError ? err.message : "Could not sign in."]);
    } finally { setBusy(false); }
  };

  return (
    <main className="auth">
      <form className="card auth-card" onSubmit={submit}>
        <h1>Larkspur Clinic Ops</h1>
        <p className="muted">Sign in to the front desk and billing portal.</p>
        <Problems items={problems} />
        <div className="field"><label htmlFor="username">Username</label>
          <input id="username" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus /></div>
        <div className="field"><label htmlFor="password">Password</label>
          <input id="password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} /></div>
        <button type="submit" className="btn btn-primary btn-block" disabled={busy}>{busy ? "Signing in..." : t("Sign In")}</button>
      </form>
    </main>
  );
}
