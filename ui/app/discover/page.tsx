"use client";
import Link from "next/link";
import { useApi, useAuth } from "@/lib/hooks";
import { atLeast, type DiscoverOptions, type DiscoverSession } from "@/lib/types";
import { ago } from "@/lib/format";
import { Empty, ErrorState, PageHead, Skeleton } from "@/components/ui";
import { DiscoverPill } from "@/components/Discover";
import { IconAlert, IconBolt, IconSpinner } from "@/components/icons";

export default function DiscoverPage() {
  const { me } = useAuth();
  const list = useApi<{ sessions: DiscoverSession[] }>("/v1/discover", { every: 5000 });
  const opts = useApi<DiscoverOptions>("/v1/discover/options", { every: 10000 });
  if (!atLeast(me?.role, "supervisor")) return <div className="page"><Empty title="Discovery needs a supervisor">Ask a supervisor to discover the new task.</Empty></div>;
  const o = opts.data;
  return (
    <div className="page">
      <PageHead title="Discover a new task" sub="Say what you want done. The model works out the clicks once on a practice system, you review the recording, and from then on every run replays it with no model.">
        <Link className="btn primary" href="/discover/new/" aria-disabled={o?.busy || !o?.model_available}><IconBolt width={16} height={16} /> Discover a task</Link>
      </PageHead>
      {o && !o.model_available && <div className="banner warn" role="status"><IconAlert width={20} height={20} /><div className="grow"><strong>No model is configured</strong><span>Set <code>GEMINI_API_KEY</code> on the server to discover tasks. Running tasks does not need it.</span></div></div>}
      {o?.busy && <div className="banner info" role="status"><IconSpinner width={20} height={20} /><div className="grow"><strong>A session is in progress</strong><span>One task is discovered at a time. Open it below, or wait for it to finish.</span></div></div>}
      {list.error && !list.data ? <ErrorState error={list.error} retry={list.reload} /> : !list.data ? <Skeleton lines={4} /> : list.data.sessions.length === 0 ? (
        <div className="card"><Empty icon={<IconBolt />} title="Nothing discovered yet">Discovery works on a practice system first, so nothing real is changed while the model is learning.</Empty></div>
      ) : (
        <section className="card"><div className="table-wrap"><table className="t"><thead><tr><th>Task</th><th>Status</th><th>Discovered by</th><th>When</th><th className="num">Steps</th></tr></thead><tbody>
          {list.data.sessions.map((s) => (
            <tr key={s.id}><td><Link className="rowlink" href={`/discover/session/?id=${s.id}`}>{s.name}</Link><div className="small muted mono">{s.capability_id}{s.version ? ` v${s.version}` : ""}</div></td>
              <td><DiscoverPill status={s.status} /></td><td>{s.created_by}</td><td>{ago(s.created_at)}</td><td className="num">{s.steps ?? "—"}</td></tr>))}
        </tbody></table></div></section>
      )}
    </div>
  );
}
