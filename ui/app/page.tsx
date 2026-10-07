"use client";
import Link from "next/link";
import { useMemo, useState } from "react";
import { actionableCount } from "@/lib/access";
import { useApi, useAuth } from "@/lib/hooks";
import { atLeast, type Capability, type RunView } from "@/lib/types";
import { ago } from "@/lib/format";
import { Empty, ErrorState, Skeleton, StatusPill } from "@/components/ui";
import { IconGrid, IconInbox, IconSearch } from "@/components/icons";

const GROUPS: { key: string; title: string; hint: string; match: (c: Capability) => boolean }[] = [
  { key: "read", title: "Look something up", hint: "Only reads. Runs straight away.", match: (c) => c.risk.level === "read_only" && !c.risk.has_irreversible_step },
  { key: "change", title: "Change something", hint: "Changes data you can correct. Runs straight away.", match: (c) => c.risk.level === "state_changing" && !c.risk.has_irreversible_step },
  { key: "commit", title: "Needs an approval first", hint: "Can't be undone from here, so a person approves it before it runs.", match: (c) => c.risk.has_irreversible_step },
];

function TaskCard({ c }: { c: Capability }) {
  return (
    <Link href={`/task/?id=${encodeURIComponent(c.capability_id)}`} className="card cap-card" style={{ textDecoration: "none", color: "inherit" }}>
      <strong style={{ fontSize: 15 }}>{c.name}</strong>
      <p className="muted small">{c.description}</p>
      {c.risk.has_irreversible_step && <div className="small muted">Approved by a {c.risk.approval_required}</div>}
    </Link>
  );
}

export default function Home() {
  const { me } = useAuth();
  const [q, setQ] = useState("");
  const caps = useApi<{ capabilities: Capability[] }>("/v1/capabilities", { every: 60000 });
  const mine = useApi<{ runs: RunView[] }>(me ? `/v1/runs?requested_by=${encodeURIComponent(me.name)}&limit=5` : null, { every: 8000 });
  const inbox = useApi<Parameters<typeof actionableCount>[0] & object>(atLeast(me?.role, "operator") ? "/v1/inbox" : null, { every: 10000 });
  const waiting = actionableCount(inbox.data, me);
  const tasks = useMemo(() => (caps.data?.capabilities ?? []).filter((c) => c.login !== null), [caps.data]);
  const found = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return needle ? tasks.filter((c) => `${c.name} ${c.description} ${c.capability_id}`.toLowerCase().includes(needle)) : null;
  }, [tasks, q]);
  const canRun = atLeast(me?.role, "operator");

  return (
    <div className="page">
      <div className="stack" style={{ gap: 14, maxWidth: 720 }}>
        <h1 style={{ fontSize: 28 }}>What do you want to do?</h1>
        <div style={{ position: "relative" }}>
          <IconSearch width={18} height={18} style={{ position: "absolute", left: 12, top: 13, color: "var(--muted)" }} />
          <input data-search autoFocus type="search" aria-label="Search tasks" placeholder="Search tasks, for example “refund” or “patient”" value={q} onChange={(e) => setQ(e.target.value)}
            style={{ paddingLeft: 38, minHeight: 44, fontSize: 16 }} />
        </div>
        {!canRun && <p className="small muted">You can look at tasks and runs, but your key can't start them.</p>}
      </div>

      {waiting > 0 && (
        <div className="banner wait" role="status"><IconInbox width={20} height={20} /><div className="grow"><strong>{waiting} {waiting === 1 ? "thing is" : "things are"} waiting for you</strong><span>Approvals and decisions only you can make.</span></div>
          <Link href="/inbox/" className="btn sm">Open inbox</Link></div>
      )}

      {caps.error ? <ErrorState error={caps.error} retry={caps.reload} />
        : !caps.data ? <div className="grid-3">{[0, 1, 2].map((i) => <div key={i} className="card cap-card"><Skeleton lines={2} /></div>)}</div>
        : found ? (found.length === 0 ? <div className="card"><Empty icon={<IconGrid />} title="Nothing matches that">Try a different word, or clear the search.</Empty></div> : <div className="grid-3">{found.map((c) => <TaskCard key={c.capability_id} c={c} />)}</div>)
        : GROUPS.map((g) => {
          const list = tasks.filter(g.match);
          return list.length === 0 ? null : (
            <section key={g.key} className="stack" aria-labelledby={`g-${g.key}`}>
              <div><h2 id={`g-${g.key}`}>{g.title}</h2><p className="small muted">{g.hint}</p></div>
              <div className="grid-3">{list.map((c) => <TaskCard key={c.capability_id} c={c} />)}</div>
            </section>
          );
        })}

      {mine.data && mine.data.runs.length > 0 && (
        <section className="card" aria-labelledby="mine">
          <div className="card-head"><h2 id="mine">Your recent runs</h2><Link href="/runs/" className="small">See all →</Link></div>
          <table className="t"><tbody>{mine.data.runs.map((r) => (
            <tr key={r.id}><td><Link className="rowlink" href={`/run/?id=${r.id}`}>{tasks.find((t) => t.capability_id === r.capability_id)?.name ?? r.capability_id}</Link></td><td><StatusPill status={r.status} /></td><td className="muted num">{ago(r.created_at)}</td></tr>
          ))}</tbody></table>
        </section>
      )}
    </div>
  );
}
