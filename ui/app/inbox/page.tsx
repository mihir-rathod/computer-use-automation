"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { useApi, useAuth, useNow, useToast } from "@/lib/hooks";
import { atLeast, type PendingApproval, type RunView, type Candidate } from "@/lib/types";
import { age, formatValue, label } from "@/lib/format";
import { Empty, ErrorState, PageHead, Skeleton } from "@/components/ui";
import { ApprovalActions, ReasonDialog, ResolveActions, Shot, Lightbox, useDecide } from "@/components/Panels";
import { IconCheck } from "@/components/icons";

interface RepairItem { id: string; capability_id: string; step_id: string; run_id: string | null; created_at: string; confident: boolean; reason: string; touches_irreversible_step: boolean; old_locators: { strategy: string; value: string }[]; new_locators: { strategy: string; value: string }[]; candidates: Candidate[]; has_screenshot: boolean; page_url: string | null; description: string }
interface Inbox { approvals: PendingApproval[]; needs_review: RunView[]; repairs: RepairItem[]; paused: RunView[]; counts: { approvals: number; needs_review: number; repairs: number; paused: number; total: number } }

const loc = (l: { strategy: string; value: string }) => `${l.strategy}: ${l.value}`;

function RepairCard({ r, onDone, onShot }: { r: RepairItem; onDone: () => void; onShot: (u: string, a: string) => void }) {
  const { me } = useAuth();
  const decide = useDecide(onDone);
  const [mode, setMode] = useState<null | "approve" | "reject">(null);
  const needed = r.touches_irreversible_step ? "supervisor" : "operator";
  const allowed = atLeast(me?.role, needed) && r.confident;
  return (
    <article className="card card-pad stack" style={{ gap: 12 }}>
      <div className="row"><strong>{r.capability_id}</strong><span className="tag">step {r.step_id}</span>
        {r.touches_irreversible_step && <span className="tag danger">commits something</span>}
        <span className={`pill ${r.confident ? "ok" : "warn"}`}>{r.confident ? "Clear match" : "No clear match"}</span><span className="spacer" /><span className="small muted">{age(r.created_at)} old</span></div>
      <p className="small">A replay could not find <strong>“{r.description}”</strong> on the page. {r.reason}</p>
      <div className="grid-2" style={{ gap: 12 }}>
        <div className="stack small" style={{ gap: 6 }}><div className="label">It looked for</div>{r.old_locators.map((l, i) => <code key={i} className="diff-del">− {loc(l)}</code>)}
          {r.new_locators.length > 0 && <><div className="label" style={{ marginTop: 6 }}>Proposed (old ones kept as fallbacks)</div>{r.new_locators.slice(0, 3).map((l, i) => <code key={i} className="diff-add">+ {loc(l)}</code>)}</>}</div>
        {r.has_screenshot && <div className="stack" style={{ gap: 6 }}><div className="label">The page right then</div><Shot big path={`/v1/repairs/${r.id}/screenshot`} alt={`Page where ${r.description} was not found`} onOpen={onShot} /></div>}
      </div>
      {r.candidates.length > 0 && <details><summary className="small muted" style={{ cursor: "pointer" }}>Other candidates considered</summary>
        <table className="t small"><thead><tr><th>Element</th><th className="num">Score</th></tr></thead><tbody>{r.candidates.map((c) => <tr key={c.ref}><td>{c.role} {c.name ? `“${c.name}”` : ""}</td><td className="num">{c.score.toFixed(2)}</td></tr>)}</tbody></table></details>}
      <div className="row">
        <button className="btn sm primary" disabled={!allowed} onClick={() => setMode("approve")} title={!r.confident ? "There is no confident match to approve" : !allowed ? `This needs a ${needed}` : undefined}>Approve repair</button>
        <button className="btn sm danger" disabled={!atLeast(me?.role, "operator")} onClick={() => setMode("reject")}>Reject</button>
        {run_link(r)}{!r.confident && <span className="small muted">Nothing to approve: fix the artifact by hand or re-record it.</span>}
      </div>
      <ReasonDialog open={mode === "approve"} onClose={() => setMode(null)} title="Approve this repair" confirm="Approve and promote"
        intro={<p>This creates a new artifact version that targets the element above and makes it current. You can roll it back from the artifact page.{r.touches_irreversible_step && <> This step <strong>commits something</strong>, so it needs a supervisor.</>}</p>}
        onSubmit={(reason) => decide(`/v1/repairs/${r.id}/approve`, { reason }, "Repair approved. A new version is current.")} />
      <ReasonDialog open={mode === "reject"} onClose={() => setMode(null)} title="Reject this repair" confirm="Reject" danger onSubmit={(reason) => decide(`/v1/repairs/${r.id}/reject`, { reason }, "Repair rejected.")} />
    </article>
  );
}
const run_link = (r: RepairItem) => r.run_id ? <Link className="small" href={`/run/?id=${r.run_id}`}>See the run that failed</Link> : null;

export default function InboxPage() {
  const inbox = useApi<Inbox>("/v1/inbox", { every: 6000 });
  const now = useNow(30000);
  const [tab, setTab] = useState<"paused" | "approvals" | "needs_review" | "repairs">("approvals");
  const [picked, setPicked] = useState(false);
  const [shot, setShot] = useState<{ url: string; alt: string } | null>(null);
  const d = inbox.data;
  // land on the most urgent thing waiting: a run that is stuck right now, before approvals
  useEffect(() => { if (d && !picked && d.counts.total > 0) { setPicked(true); if (d.counts.paused > 0) setTab("paused"); else if (d.counts.approvals === 0 && d.counts.needs_review > 0) setTab("needs_review"); } }, [d, picked]);
  const toast = useToast(); void toast;
  const tabBtn = (key: typeof tab, text: string, n?: number) => <button role="tab" aria-selected={tab === key} className="tab" onClick={() => setTab(key)}>{text}{n ? <span className="pill wait" style={{ marginLeft: 8 }}>{n}</span> : null}</button>;
  return (
    <div className="page">
      <PageHead title="Inbox" sub="Everything waiting for a person: runs that got stuck, approvals before something is committed, runs whose outcome is unknown, and proposed repairs after a screen changed." />
      {inbox.error && !d ? <ErrorState error={inbox.error} retry={inbox.reload} /> : !d ? <Skeleton lines={4} /> : d.counts.total === 0 ? (
        <div className="card"><Empty icon={<IconCheck />} title="You're all caught up">Approvals, unknown outcomes and repair proposals appear here the moment they need someone.</Empty></div>
      ) : (
        <>
          <div className="tabs" role="tablist">{tabBtn("paused", "Waiting for a person", d.counts.paused)}{tabBtn("approvals", "Approvals", d.counts.approvals)}{tabBtn("needs_review", "Unknown outcomes", d.counts.needs_review)}{tabBtn("repairs", "Repairs", d.counts.repairs)}</div>
          {tab === "paused" && (d.paused.length === 0 ? <div className="card"><Empty title="Nothing is stuck" /></div> : (
            <div className="stack">{d.paused.map((r) => (
              <article key={r.id} className="card card-pad stack" style={{ gap: 10 }}>
                <div className="row"><Link className="rowlink" href={`/run/?id=${r.id}`} style={{ fontSize: 15 }}>{r.capability_id}</Link><span className="pill wait">Needs a person</span><span className="spacer" />
                  <span className="small muted">asked by <strong>{r.requested_by}</strong> · stuck {age(r.paused?.since, now)}{r.paused?.being_helped ? " · someone is helping" : ""}</span></div>
                <p className="small">{r.paused?.step_id ? `It could not do step ${r.paused.step_id}: ` : ""}{r.paused?.reason}</p>
                <div className="row"><Link className="btn sm primary" href={`/run/?id=${r.id}`}>Take over</Link>{r.paused?.stops_in_s != null && <span className="small muted">stops by itself in about {Math.max(1, Math.ceil(r.paused.stops_in_s / 60))} min</span>}</div>
              </article>))}</div>))}
          {tab === "approvals" && (d.approvals.length === 0 ? <div className="card"><Empty title="No approvals waiting" /></div> : (
            <div className="stack">{d.approvals.map((a) => (
              <article key={a.run_id} className="card card-pad stack" style={{ gap: 12 }}>
                <div className="row"><Link className="rowlink" href={`/run/?id=${a.run_id}`} style={{ fontSize: 15 }}>{a.capability_id}</Link><span className="pill wait">Needs a {a.tier}</span>{a.at_step && <span className="tag">stopped at the step</span>}<span className="spacer" />
                  <span className="small muted">asked by <strong>{a.requested_by}</strong> · waiting {age(a.requested_at, now)}</span></div>
                <dl className="kv small">{Object.entries(a.params).map(([k, v]) => <div key={k} style={{ display: "contents" }}><dt>{label(k)}</dt><dd>{formatValue(v)}</dd></div>)}</dl>
                {a.at_step
                  ? <div className="row"><Link className="btn sm primary" href={`/run/?id=${a.run_id}`}>Review and decide</Link><span className="small muted">It stopped before {a.description ?? "its last step"}; you see the page before deciding.{a.stops_in_s != null && ` Stops by itself in about ${Math.max(1, Math.ceil(a.stops_in_s / 60))} min.`}</span></div>
                  : <ApprovalActions run={{ id: a.run_id, requested_by: a.requested_by }} tier={a.tier} onDone={inbox.reload} compact />}
              </article>))}</div>))}
          {tab === "needs_review" && (d.needs_review.length === 0 ? <div className="card"><Empty title="No unknown outcomes" /></div> : (
            <div className="stack">{d.needs_review.map((r) => (
              <article key={r.id} className="card card-pad stack" style={{ gap: 12 }}>
                <div className="row"><Link className="rowlink" href={`/run/?id=${r.id}`} style={{ fontSize: 15 }}>{r.capability_id}</Link><span className="pill warn">Outcome unknown</span><span className="spacer" /><span className="small muted">{age(r.created_at, now)} old</span></div>
                <p className="small">The final step was issued but the page never confirmed it, so it was <strong>not retried</strong>. Check the target system, then record what you found: until then, the same idempotency key is blocked.</p>
                <dl className="kv small">{Object.entries(r.params).map(([k, v]) => <div key={k} style={{ display: "contents" }}><dt>{label(k)}</dt><dd>{formatValue(v)}</dd></div>)}</dl>
                <div><ResolveActions run={r} onDone={inbox.reload} compact /></div>
              </article>))}</div>))}
          {tab === "repairs" && (d.repairs.length === 0 ? <div className="card"><Empty title="No repair proposals" /></div> : <div className="stack">{d.repairs.map((r) => <RepairCard key={r.id} r={r} onDone={inbox.reload} onShot={(url, alt) => setShot({ url, alt })} />)}</div>)}
        </>
      )}
      <Lightbox shot={shot} onClose={() => setShot(null)} />
    </div>
  );
}
