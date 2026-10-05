"use client";
import Link from "next/link";
import { useApi } from "@/lib/hooks";
import type { Capability } from "@/lib/types";
import { ErrorState, PageHead, Skeleton, Empty } from "@/components/ui";
import { IconFile } from "@/components/icons";

export default function Artifacts() {
  const caps = useApi<{ capabilities: Capability[] }>("/v1/capabilities");
  const list = caps.data?.capabilities ?? [];
  return (
    <div className="page">
      <PageHead title="Artifacts" sub="The recorded form of each capability: its steps, how each element is found, who approved the irreversible step, and every version." />
      <section className="card">
        {caps.error ? <div className="card-body"><ErrorState error={caps.error} retry={caps.reload} /></div> : !caps.data ? <div className="card-body"><Skeleton lines={5} /></div>
          : list.length === 0 ? <Empty icon={<IconFile />} title="No artifacts yet" />
          : <div className="table-wrap"><table className="t"><thead><tr><th>Capability</th><th>Current</th><th className="num">Versions</th><th>Reviewed</th><th>System</th></tr></thead><tbody>
            {list.map((c) => <tr key={c.capability_id}><td><Link className="rowlink" href={`/artifact/?id=${encodeURIComponent(c.capability_id)}`}>{c.capability_id}</Link>{c.login === null && <span className="tag" style={{ marginLeft: 8 }}>sign-on</span>}</td>
              <td className="mono">{c.version}</td><td className="num">{c.versions.length}</td><td>{c.reviewed ? "Yes" : <span className="muted">Not yet</span>}</td><td>{c.app}</td></tr>)}</tbody></table></div>}
      </section>
    </div>
  );
}
