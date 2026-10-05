"use client";
import { useState, type FormEvent } from "react";
import { useAuth } from "@/lib/hooks";
import { Field } from "./ui";

export function SignIn() {
  const { signIn } = useAuth();
  const [key, setKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError(null);
    try { await signIn(key); } catch (err) { setError(err instanceof Error ? err.message : "Sign-in failed"); } finally { setBusy(false); }
  }
  return (
    <div style={{ minHeight: "100vh", display: "grid", placeItems: "center", padding: 20 }}>
      <form onSubmit={submit} className="card card-pad stack" style={{ width: "min(440px, 100%)", gap: 18 }}>
        <div className="brand" style={{ padding: 0 }}><span className="brand-mark" aria-hidden>CU</span><span>Capability Console</span></div>
        <div><h1 style={{ fontSize: 20 }}>Sign in with your API key</h1>
          <p className="muted" style={{ marginTop: 6 }}>Your key identifies you: its name is recorded on everything you submit or approve, and its role decides what you may do.</p></div>
        <Field id="key" label="API key" error={error} hint={!error ? "Starts with cua_. An administrator creates it with: uv run python cli.py keys create" : null}>
          <input id="key" type="password" autoComplete="off" autoFocus value={key} onChange={(e) => setKey(e.target.value)} aria-invalid={!!error} aria-describedby={error ? "key-err" : "key-hint"} placeholder="cua_…" />
        </Field>
        <button className="btn primary" disabled={!key.trim() || busy}>{busy ? "Checking…" : "Sign in"}</button>
      </form>
    </div>
  );
}
