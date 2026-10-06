"use client";
import { useApi } from "@/lib/hooks";
import type { PolicyView } from "@/lib/types";
import { label } from "@/lib/format";
import { ErrorState, PageHead, Skeleton } from "@/components/ui";

export default function Policy() {
  const p = useApi<PolicyView>("/v1/policy", { every: 30000 });
  if (p.error) return <div className="page"><ErrorState error={p.error} retry={p.reload} /></div>;
  if (!p.data) return <div className="page"><Skeleton lines={6} /></div>;
  const d = p.data;
  return (
    <div className="page">
      <PageHead title="Safety policy" sub={<>How commits are authorised. This view is read-only: change it by editing <code>safety/policy.yaml</code>; the file is re-read on every run, so a change applies to the next one.</>} />
      <section className="card"><div className="card-head"><h2>Approval tiers</h2></div><div className="card-body"><dl className="kv">{Object.entries(d.approval_tiers).map(([k, v]) => <div key={k} style={{ display: "contents" }}><dt><span className="tag">{k}</span></dt><dd>{v}</dd></div>)}</dl></div></section>
      <section className="card"><div className="card-head"><h2>Per capability</h2></div>
        <div className="table-wrap"><table className="t"><thead><tr><th>Capability</th><th>Approval</th><th>Largest amounts</th><th className="num">Commits a day</th></tr></thead><tbody>
          {Object.entries(d.capabilities).map(([id, c]) => <tr key={id}><td className="mono">{id}</td><td><span className="tag">{c.approval}</span></td>
            <td>{Object.keys(c.caps.max_param).length ? Object.entries(c.caps.max_param).map(([k, v]) => `${label(k)} ≤ ${v}`).join(", ") : <span className="muted">no limit</span>}</td><td className="num">{c.caps.max_commits_per_day ?? "—"}</td></tr>)}
        </tbody></table></div>
        <div className="card-body small muted">A capability not listed here defaults to <span className="tag">{d.default_approval}</span>{d.default_approval === "live" ? ": its irreversible step is blocked until a person confirms it during the run" : ": a task with an irreversible step waits for that kind of approval before it runs"}. Tasks discovered from the console land here, so a new task is never less guarded than the default.</div></section>
      <div className="grid-2">
        <section className="card"><div className="card-head"><h2>People who may approve</h2></div><div className="card-body">
          <p className="small muted" style={{ marginBottom: 8 }}>Used by the command line. Over the API, your key's role decides.</p>
          <dl className="kv">{Object.entries(d.approvers).map(([n, r]) => <div key={n} style={{ display: "contents" }}><dt>{n}</dt><dd>{r}</dd></div>)}</dl></div></section>
        <section className="card"><div className="card-head"><h2>Evidence</h2></div><div className="card-body"><dl className="kv">
          <dt>Screenshots</dt><dd>{d.evidence.screenshots === "every_action" ? "After every action" : "Only when an action fails"}{!d.evidence.non_sandbox && " (sandbox targets only)"}</dd>
          <dt>Debug traces</dt><dd>{d.tracing.mode === "off" ? "Off" : d.tracing.mode === "always" ? "Every run" : "Failed runs only"}{!d.tracing.non_sandbox && d.tracing.mode !== "off" && " (sandbox targets only)"}</dd>
          <dt>Redacted fields</dt><dd>{d.redaction.field_names.join(", ") || "—"}</dd><dt>Redacted patterns</dt><dd>{d.redaction.patterns.join(", ") || "—"}</dd>
          <dt>Extra risk words</dt><dd>{[...d.risk_keywords.commit, ...d.risk_keywords.domain].join(", ") || "—"}</dd></dl>
          <p className="small muted" style={{ marginTop: 8 }}>Screenshots and traces cannot be redacted, which is why they are limited to sandbox targets.</p></div></section>
      </div>
    </div>
  );
}
