"use client";
import Link from "next/link";
import { useMemo, useState } from "react";
import { useApi, useAuth } from "@/lib/hooks";
import type { Capability, RunView } from "@/lib/types";
import { ago, duration } from "@/lib/format";
import { Empty, ErrorState, PageHead, Skeleton, StatusPill, isActive } from "@/components/ui";
import { IconPlay, IconSearch } from "@/components/icons";

const STATUSES = ["running", "pending_approval", "success", "business_outcome", "hard_failure", "needs_review", "dry_run"];
const NAMES: Record<string, string> = { running: "Running", pending_approval: "Awaiting approval", success: "Succeeded", business_outcome: "Answered", hard_failure: "Failed", needs_review: "Needs review", dry_run: "Dry run" };

export default function Runs() {
  const { me } = useAuth();
  const [status, setStatus] = useState("");
  const [cap, setCap] = useState("");
  const [q, setQ] = useState("");
  const defaultMine = me?.role === "operator" || me?.role === "supervisor";
  const [mine, setMine] = useState(defaultMine);
  const [limit, setLimit] = useState(50);
  const caps = useApi<{ capabilities: Capability[] }>("/v1/capabilities");
  const path = useMemo(() => {
    const p = new URLSearchParams({ limit: String(limit) });
    if (status) p.set("status", status); if (cap) p.set("capability_id", cap); if (q.trim()) p.set("q", q.trim()); if (mine && me) p.set("requested_by", me.name);
    return `/v1/runs?${p}`;
  }, [status, cap, q, mine, limit, me]);
  const runs = useApi<{ runs: RunView[] }>(path, { every: 5000 });
  const list = runs.data?.runs ?? [];
  const filtered = status === "" && cap === "" && q === "" && mine === defaultMine;
  return (
    <div className="page">
      <PageHead title={me?.role === "operator" || me?.role === "supervisor" ? "My runs" : "Runs"} sub="Newest first. Open one to see its steps, what the page looked like, and who approved it." />
      <div className="row">
        <div style={{ position: "relative", flex: "1 1 240px", maxWidth: 340 }}>
          <IconSearch width={16} height={16} style={{ position: "absolute", left: 10, top: 10, color: "var(--muted)" }} />
          <input data-search type="search" aria-label="Search runs" placeholder="Search by task, run id or person  (/)" value={q} onChange={(e) => { setQ(e.target.value); setLimit(50); }} style={{ paddingLeft: 32 }} />
        </div>
        <select aria-label="Task" value={cap} onChange={(e) => setCap(e.target.value)} style={{ width: "auto" }}>
          <option value="">All tasks</option>{(caps.data?.capabilities ?? []).filter((c) => c.login !== null).map((c) => <option key={c.capability_id} value={c.capability_id}>{c.name}</option>)}
        </select>
        <label className="row small" style={{ gap: 6 }}><input type="checkbox" checked={mine} onChange={(e) => setMine(e.target.checked)} /> Only mine</label>
      </div>
      <div className="chips" role="group" aria-label="Filter by status">
        <button className="chip" aria-pressed={status === ""} onClick={() => setStatus("")}>All</button>
        {STATUSES.map((s) => <button key={s} className="chip" aria-pressed={status === s} onClick={() => setStatus(status === s ? "" : s)}>{NAMES[s]}</button>)}
      </div>
      <section className="card">
        {runs.error ? <div className="card-body"><ErrorState error={runs.error} retry={runs.reload} /></div>
          : !runs.data ? <div className="card-body"><Skeleton lines={5} /></div>
          : list.length === 0 ? <Empty icon={<IconPlay />} title={filtered ? "No runs yet" : "No runs match these filters"}>{filtered ? <>Start one from the <Link href="/">task list</Link>.</> : "Try clearing a filter."}</Empty>
          : <div className="table-wrap"><table className="t"><thead><tr><th>Task</th><th>Status</th><th>Requested by</th><th>Started</th><th className="num">Took</th><th>Run</th></tr></thead>
            <tbody>{list.map((r) => (
              <tr key={r.id}>
                <td><Link className="rowlink" href={`/run/?id=${r.id}`}>{r.capability_id}</Link>{r.committed && <span className="tag danger" style={{ marginLeft: 8 }}>committed</span>}</td>
                <td><StatusPill status={r.status} paused={!!r.paused} /></td><td>{r.requested_by}</td><td className="muted">{ago(r.created_at)}</td>
                <td className="num muted">{isActive(r.status) ? "…" : duration(r.started_at, r.finished_at)}</td><td className="mono muted small">{r.id.replace("run_", "").slice(0, 22)}</td>
              </tr>))}</tbody></table></div>}
        {list.length >= limit && <div className="card-body"><button className="btn" onClick={() => setLimit(limit + 50)}>Show more</button></div>}
      </section>
    </div>
  );
}
