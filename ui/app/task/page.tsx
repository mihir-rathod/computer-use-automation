"use client";
import Link from "next/link";
import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { useApi, useAuth } from "@/lib/hooks";
import { atLeast, type Capability, type RunView, type Target } from "@/lib/types";
import { ago, label } from "@/lib/format";
import { Empty, ErrorState, PageHead, RiskBadge, Skeleton, StatusPill } from "@/components/ui";
import { TaskForm } from "@/components/TaskForm";

function Task() {
  const id = useSearchParams().get("id") ?? "";
  const { me } = useAuth();
  const cap = useApi<Capability>(id ? `/v1/capabilities/${encodeURIComponent(id)}` : null);
  const targets = useApi<{ targets: Target[] }>("/v1/targets");
  const runs = useApi<{ runs: RunView[] }>(id ? `/v1/runs?capability_id=${encodeURIComponent(id)}&limit=5` : null, { every: 8000 });
  if (!id) return <div className="page"><Empty title="No task chosen">Pick one from the <Link href="/">task list</Link>.</Empty></div>;
  if (cap.error) return <div className="page"><ErrorState error={cap.error} retry={cap.reload} /></div>;
  if (!cap.data || !targets.data) return <div className="page"><Skeleton lines={5} /></div>;
  const c = cap.data;
  const caps = c.risk.caps;
  return (
    <div className="page">
      <PageHead title={c.name} crumb={<Link href="/">← Tasks</Link>} sub={c.description}><RiskBadge level={c.risk.level} commits={c.risk.has_irreversible_step} tier={c.risk.approval_required} /></PageHead>
      <div className="split">
        <TaskForm key={c.capability_id} cap={c} targets={targets.data.targets} />
        <div className="stack">
          <section className="card"><div className="card-head"><h2>What to expect</h2></div><div className="card-body stack">
            {c.risk.has_irreversible_step ? <p>This <strong>commits something that cannot be undone from here</strong>. It is held until a {c.risk.approval_required} approves it, and a retry with the same key cannot do it twice.</p>
              : c.risk.level === "state_changing" ? <p>This changes data but can be corrected, and runs straight away.</p> : <p>This only reads. It runs straight away and changes nothing.</p>}
            {caps && (Object.keys(caps.max_param).length > 0 || caps.max_commits_per_day) && <p className="small muted">Limits: {Object.entries(caps.max_param).map(([k, v]) => `${label(k)} up to ${v}`).join(", ")}{caps.max_commits_per_day ? `${Object.keys(caps.max_param).length ? "; " : ""}${caps.max_commits_per_day} a day` : ""}.</p>}
            <div><div className="label">You get back</div><div className="chips" style={{ marginTop: 6 }}>{Object.keys(c.output_schema.properties).map((k) => <span key={k} className="tag">{label(k)}</span>)}</div></div>
          </div></section>
          <section className="card"><div className="card-head"><h2>Details</h2></div><div className="card-body"><dl className="kv small">
            <dt>Version</dt><dd>{c.version}{c.versions.length > 1 && <span className="muted"> ({c.versions.length} versions)</span>}</dd>
            <dt>System</dt><dd>{c.app}</dd><dt>Reviewed</dt><dd>{c.reviewed ? "Yes" : "Not yet by a person"}</dd>
            <dt>Last check</dt><dd>{c.last_canary ? `${c.last_canary.ok ? "passed" : "FAILED"} ${ago(c.last_canary.at)}` : c.has_canary ? "not run yet" : "none scheduled"}</dd>
          </dl>{atLeast(me?.role, "admin") && <p className="small" style={{ marginTop: 10 }}><Link href={`/artifact/?id=${encodeURIComponent(c.capability_id)}`}>Inspect the recorded steps →</Link></p>}</div></section>
          <section className="card"><div className="card-head"><h2>Recent runs</h2></div>
            {!runs.data ? <div className="card-body"><Skeleton lines={2} /></div> : runs.data.runs.length === 0 ? <Empty title="Not run yet" />
              : <table className="t"><tbody>{runs.data.runs.map((r) => <tr key={r.id}><td><Link href={`/run/?id=${r.id}`}>{ago(r.created_at)}</Link></td><td><StatusPill status={r.status} /></td><td className="muted">{r.requested_by}</td></tr>)}</tbody></table>}
          </section>
        </div>
      </div>
    </div>
  );
}
export default function Page() { return <Suspense fallback={<div className="page"><Skeleton lines={5} /></div>}><Task /></Suspense>; }
