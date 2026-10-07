"use client";
import Link from "next/link";
import { useApi, useAuth } from "@/lib/hooks";
import { ApiError } from "@/lib/api";
import { atLeast } from "@/lib/types";
import type { Metrics, RunView } from "@/lib/types";
import { ago, pct, secs } from "@/lib/format";
import { Empty, ErrorState, PageHead, Skeleton, StatusPill, CapLink } from "@/components/ui";
import { IconCheck, IconAlert, IconInbox, IconPlay } from "@/components/icons";

export default function Overview() {
  const { me } = useAuth();
  if (!atLeast(me?.role, "admin")) return <div className="page"><ErrorState error={new ApiError(403, "The overview and its metrics are for administrators.")} /></div>;
  return <OverviewBody />;
}

function OverviewBody() {
  const metrics = useApi<Metrics>("/v1/metrics?since_hours=168", { every: 15000 });
  const recent = useApi<{ runs: RunView[] }>("/v1/runs?limit=8", { every: 8000 });
  const inbox = useApi<{ counts: { approvals: number; needs_review: number; repairs: number; total: number } }>("/v1/inbox", { every: 10000 });
  const m = metrics.data;
  const c = inbox.data?.counts;
  return (
    <div className="page">
      <PageHead title="Overview" sub="How the platform has done over the last 7 days, and what is waiting for a person." />

      {c && c.total > 0 ? (
        <div className="banner wait" role="status"><IconInbox width={20} height={20} /><div className="grow"><strong>{c.total} {c.total === 1 ? "item needs" : "items need"} a person</strong>
          <span>{[c.approvals && `${c.approvals} awaiting approval`, c.needs_review && `${c.needs_review} with an unknown outcome`, c.repairs && `${c.repairs} repair ${c.repairs === 1 ? "proposal" : "proposals"}`].filter(Boolean).join(" · ")}</span></div>
          <Link href="/inbox/" className="btn sm">Open inbox</Link></div>
      ) : c ? (
        <div className="banner ok" role="status"><IconCheck width={20} height={20} /><div className="grow"><strong>Nothing is waiting for you</strong><span>No approvals, unresolved commits or repair proposals.</span></div></div>
      ) : null}
      {m && m.totals.failing_canaries.length > 0 && (
        <div className="banner warn" role="alert"><IconAlert width={20} height={20} /><div className="grow"><strong>A canary is failing</strong>
          <span>{m.totals.failing_canaries.join(", ")} did not pass its last scheduled check, which usually means the target's screens changed.</span></div></div>
      )}

      <div className="grid-3">
        {[["Runs (7 days)", m?.totals.runs], ["Awaiting approval", c?.approvals], ["Unknown outcome", c?.needs_review], ["Repair proposals", c?.repairs]].map(([label, n]) => (
          <div key={String(label)} className="card tile"><span className="n">{n ?? "—"}</span><span className="l">{label}</span></div>
        ))}
      </div>

      <section className="card" aria-labelledby="by-cap">
        <div className="card-head"><h2 id="by-cap">By capability</h2><Link href="/runs/" className="small">All runs →</Link></div>
        {metrics.error ? <div className="card-body"><ErrorState error={metrics.error} retry={metrics.reload} /></div>
          : !m ? <div className="card-body"><Skeleton lines={4} /></div>
          : m.capabilities.length === 0 ? <Empty icon={<IconPlay />} title="No runs yet">Run a task from the home page and its figures appear here.</Empty>
          : <div className="table-wrap"><table className="t"><thead><tr><th>Capability</th><th className="num">Runs</th><th>Success</th><th className="num">Failed</th><th className="num">Needs review</th><th className="num">Escalated</th><th className="num">p50</th><th className="num">p95</th></tr></thead>
            <tbody>{m.capabilities.map((x) => (
              <tr key={x.capability_id}><td><CapLink id={x.capability_id} /></td><td className="num">{x.runs}</td>
                <td><div className="row" style={{ gap: 8 }}><div className="bar" aria-hidden><span style={{ width: `${(x.success_rate ?? 0) * 100}%` }} /></div><span style={{ minWidth: 38 }}>{pct(x.success_rate)}</span></div></td>
                <td className="num">{pct(x.failure_rate)}</td><td className="num">{x.needs_review || "—"}</td><td className="num">{pct(x.escalation_rate)}</td>
                <td className="num">{secs(x.latency_s.p50)}</td><td className="num">{secs(x.latency_s.p95)}</td></tr>
            ))}</tbody></table></div>}
        <div className="card-body small muted">Success counts a correct answer such as “no such patient” as a good result. Runs waiting for approval or never executed are left out of the rates.</div>
      </section>

      <section className="card" aria-labelledby="recent">
        <div className="card-head"><h2 id="recent">Recent runs</h2></div>
        {recent.error ? <div className="card-body"><ErrorState error={recent.error} retry={recent.reload} /></div>
          : !recent.data ? <div className="card-body"><Skeleton lines={3} /></div>
          : recent.data.runs.length === 0 ? <Empty title="Nothing has run yet" />
          : <div className="table-wrap"><table className="t"><tbody>{recent.data.runs.map((r) => (
            <tr key={r.id}><td><Link className="rowlink" href={`/run/?id=${r.id}`}>{r.capability_id}</Link></td><td><StatusPill status={r.status} /></td><td className="muted">{r.requested_by}</td><td className="muted num">{ago(r.created_at)}</td></tr>
          ))}</tbody></table></div>}
      </section>
    </div>
  );
}
