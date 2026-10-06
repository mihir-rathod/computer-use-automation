"use client";
import { useState, type FormEvent } from "react";
import { ApiError, api } from "@/lib/api";
import { useApi, useAuth, useToast } from "@/lib/hooks";
import { atLeast, type KeyRow, type Role } from "@/lib/types";
import { ago } from "@/lib/format";
import { Dialog, Empty, ErrorState, Field, PageHead, Skeleton } from "@/components/ui";

export default function Keys() {
  const { me } = useAuth();
  const toast = useToast();
  const keys = useApi<{ keys: KeyRow[] }>(atLeast(me?.role, "admin") ? "/v1/keys" : null);
  const [name, setName] = useState(""); const [role, setRole] = useState<Role>("operator");
  const [created, setCreated] = useState<{ name: string; key: string } | null>(null);
  const [busy, setBusy] = useState(false); const [err, setErr] = useState<string | null>(null);
  if (!atLeast(me?.role, "admin")) return <div className="page"><ErrorState error={new ApiError(403, "Managing API keys needs an admin key.")} /></div>;
  async function create(e: FormEvent) {
    e.preventDefault(); setBusy(true); setErr(null);
    try { const r = await api<{ name: string; key: string }>("/v1/keys", { method: "POST", json: { name: name.trim(), role } }); setCreated(r); setName(""); void keys.reload(); }
    catch (x) { setErr(x instanceof ApiError ? x.message : "Failed"); } finally { setBusy(false); }
  }
  async function revoke(n: string) {
    try { await api(`/v1/keys/${encodeURIComponent(n)}/revoke`, { method: "POST" }); toast(`Revoked ${n}.`); void keys.reload(); } catch (x) { toast(x instanceof ApiError ? x.message : "Failed", true); }
  }
  return (
    <div className="page">
      <PageHead title="API keys" sub="A key is a person's or a program's identity. Its name is recorded on everything it submits or approves; its role is its ceiling. Only a hash is stored, so a lost key is replaced, not recovered." />
      <form onSubmit={create} className="card card-pad"><div className="row" style={{ alignItems: "flex-end" }}>
        <div style={{ flex: "1 1 220px" }}><Field id="kn" label="Name" error={err} hint="Who or what will use it, e.g. suzie.visor or my-assistant"><input id="kn" type="text" value={name} onChange={(e) => setName(e.target.value)} maxLength={60} /></Field></div>
        <div style={{ width: 170 }}><Field id="kr" label="Role"><select id="kr" value={role} onChange={(e) => setRole(e.target.value as Role)}>{["viewer", "operator", "supervisor", "admin"].map((r) => <option key={r}>{r}</option>)}</select></Field></div>
        <button className="btn primary" disabled={!name.trim() || busy}>Create key</button></div></form>
      <section className="card">
        {keys.error ? <div className="card-body"><ErrorState error={keys.error} retry={keys.reload} /></div> : !keys.data ? <div className="card-body"><Skeleton lines={3} /></div> : keys.data.keys.length === 0 ? <Empty title="No keys" />
          : <div className="table-wrap"><table className="t"><thead><tr><th>Name</th><th>Role</th><th>Created</th><th>Last used</th><th /></tr></thead><tbody>{keys.data.keys.map((k) => (
            <tr key={k.id}><td><strong>{k.name}</strong>{k.name === me?.name && <span className="tag" style={{ marginLeft: 8 }}>you</span>}</td><td><span className="tag">{k.role}</span></td><td className="muted">{ago(k.created_at)}</td><td className="muted">{k.last_used_at ? ago(k.last_used_at) : "never"}</td>
              <td style={{ textAlign: "right" }}>{k.revoked_at ? <span className="muted small">revoked {ago(k.revoked_at)}</span> : <button className="btn sm danger" disabled={k.name === me?.name} onClick={() => revoke(k.name)} title={k.name === me?.name ? "You cannot revoke the key you are using" : undefined}>Revoke</button>}</td></tr>))}</tbody></table></div>}
      </section>
      <Dialog open={!!created} onClose={() => setCreated(null)} title={`Key for ${created?.name}`} footer={<button className="btn primary" onClick={() => setCreated(null)}>I've saved it</button>}>
        <div className="banner warn"><div className="grow"><strong>Copy it now</strong><span>This is the only time it is shown.</span></div></div>
        <input readOnly aria-label="The new key" value={created?.key ?? ""} className="mono" onFocus={(e) => e.currentTarget.select()} />
        <button className="btn" onClick={() => created && navigator.clipboard.writeText(created.key).then(() => toast("Copied."))}>Copy to clipboard</button>
      </Dialog>
    </div>
  );
}
