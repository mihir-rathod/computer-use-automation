"use client";
import Link from "next/link";
import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useApi, useAuth, useToast } from "@/lib/hooks";
import { api, ApiError } from "@/lib/api";
import { atLeast, type ArtifactDiff, type Capability } from "@/lib/types";
import { ago, when } from "@/lib/format";
import { Empty, ErrorState, PageHead, RiskBadge, Skeleton } from "@/components/ui";
import { ReasonDialog } from "@/components/Panels";

interface Versions { current: string | null; versions: string[]; history: { action: string; version: string; from: string | null; at: string; by: string; reason: string }[] }

function DiffView({ d }: { d: ArtifactDiff }) {
  const none = !d.metadata.length && !d.schema.length && !d.safety.length && !d.error_handling.length && !d.steps.length;
  if (none) return <Empty title="No differences" />;
  const Block = ({ title, items }: { title: string; items: string[] }) => items.length ? <div><div className="label">{title}</div><ul className="small" style={{ margin: "4px 0 0", paddingLeft: 18 }}>{items.map((x, i) => <li key={i}>{x}</li>)}</ul></div> : null;
  return (
    <div className="stack">
      <Block title="Details" items={d.metadata} /><Block title="Inputs and outputs" items={d.schema} /><Block title="Safety" items={d.safety} /><Block title="Error handling" items={d.error_handling} />
      {d.steps.length > 0 && <div><div className="label">Steps</div><ul className="small" style={{ margin: "4px 0 0", paddingLeft: 18 }}>{d.steps.map((s, i) => (
        <li key={i}><span className={s.kind === "added" ? "diff-add" : s.kind === "removed" ? "diff-del" : "diff-chg"}>{s.kind === "added" ? "+" : s.kind === "removed" ? "−" : "~"} {s.step}</span> {s.summary}{s.details.map((x, j) => <div key={j} className="muted mono" style={{ fontSize: 12 }}>{x}</div>)}</li>))}</ul></div>}
    </div>
  );
}

function Artifact() {
  const search = useSearchParams();
  const id = search.get("id") ?? "";
  const { me } = useAuth();
  const toast = useToast();
  const [version, setVersion] = useState<string>(search.get("version") ?? "");
  const vs = useApi<Versions>(id ? `/v1/artifacts/${encodeURIComponent(id)}/versions` : null);
  const cap = useApi<Capability>(id && (version || vs.data?.current) ? `/v1/capabilities/${encodeURIComponent(id)}${version ? `?version=${version}` : ""}` : null);
  // a draft (taught, never promoted) has no current version: show its latest instead of failing
  const draftLatest = vs.data && vs.data.current === null ? vs.data.versions[vs.data.versions.length - 1] : "";
  useEffect(() => { if (draftLatest && !version) setVersion(draftLatest); }, [draftLatest, version]);
  const [from, setFrom] = useState(""); const [to, setTo] = useState("");
  const diff = useApi<ArtifactDiff>(id && from && to && from !== to ? `/v1/artifacts/${encodeURIComponent(id)}/diff?from=${from}&to=${to}` : null);
  const [act, setAct] = useState<null | { kind: "promote" | "rollback"; v?: string }>(null);
  if (!id) return <div className="page"><Empty title="No artifact chosen">Pick one from <Link href="/artifacts/">Artifacts</Link>.</Empty></div>;
  if (cap.error) return <div className="page"><ErrorState error={cap.error} retry={cap.reload} /></div>;
  if (!cap.data || !vs.data) return <div className="page"><Skeleton lines={6} /></div>;
  const c = cap.data, v = vs.data;
  const canAct = atLeast(me?.role, "operator");
  async function run(reason: string) {
    try {
      if (act?.kind === "promote") await api(`/v1/artifacts/${encodeURIComponent(id)}/promote/${act.v}`, { method: "POST", json: { reason } });
      else await api(`/v1/artifacts/${encodeURIComponent(id)}/rollback`, { method: "POST", json: { reason } });
      toast(act?.kind === "promote" ? `Version ${act.v} is now current.` : "Rolled back."); void vs.reload(); void cap.reload();
    } catch (e) { throw e instanceof ApiError ? e : new Error("failed"); }
  }
  return (
    <div className="page">
      <PageHead title={c.name} crumb={<Link href="/artifacts/">← Artifacts</Link>} sub={<code>{c.capability_id}</code>}>
        <RiskBadge level={c.risk.level} commits={c.risk.has_irreversible_step} tier={c.risk.approval_required} />
        <select aria-label="Version" value={version || v.current || ""} onChange={(e) => setVersion(e.target.value === v.current ? "" : e.target.value)} style={{ width: "auto" }}>
          {v.versions.map((x) => <option key={x} value={x}>v{x}{x === v.current ? " (current)" : v.current === null ? " (draft)" : ""}</option>)}</select>
      </PageHead>
      <div className="split">
        <section className="card"><div className="card-head"><h2>Steps</h2><span className="small muted">v{c.version}</span></div>
          <div className="table-wrap"><table className="t small"><thead><tr><th>#</th><th>What it does</th><th>How it finds the element</th><th>Expects</th></tr></thead><tbody>
            {(c.steps ?? []).map((s, i) => <tr key={s.step_id}><td className="muted">{i + 1}</td>
              <td><strong>{s.description}</strong>{s.risk === "irreversible" && <span className="tag danger" style={{ marginLeft: 6 }}>commits</span>}{s.optional_input && <div className="muted">only if “{s.optional_input}” is given</div>}{s.output && <div className="muted">saves as {s.output}</div>}</td>
              <td>{s.locators.length ? s.locators.map((l, j) => <div key={j}><code>{l.strategy}: {l.value}</code></div>) : <span className="muted">—</span>}</td><td>{s.expected ?? <span className="muted">—</span>}</td></tr>)}
          </tbody></table></div>
          <div className="card-body small muted" style={{ borderTop: "1px solid var(--border)" }}>Successful when {c.success_when}. {(c.business_outcomes?.length ?? 0) > 0 && <>Normal answers it recognises: {c.business_outcomes!.map((b) => `“${b.outcome}” (${b.when})`).join("; ")}. </>}{(c.recoverable?.length ?? 0) > 0 && <>It recovers by itself from: {c.recoverable!.map((r) => `${r.when} → ${r.action.replace(/_/g, " ")}`).join("; ")}.</>}</div>
        </section>
        <div className="stack" style={{ gap: 16 }}>
          <section className="card"><div className="card-head"><h2>Provenance</h2></div><div className="card-body"><dl className="kv small">
            <dt>Recorded by</dt><dd>{c.provenance?.discovered_by}</dd><dt>Reviewed</dt><dd>{c.provenance?.reviewed ? `Yes${c.provenance?.approved_by ? `, by ${c.provenance.approved_by}` : ""}` : "Not yet by a person"}</dd>
            {c.provenance?.parent_version && <><dt>Derived from</dt><dd>v{c.provenance.parent_version}</dd></>}{c.provenance?.change_note && <><dt>Change</dt><dd>{c.provenance.change_note}</dd></>}
            <dt>Commit step approvals</dt><dd>{c.provenance?.commit_approvals.length ? c.provenance.commit_approvals.map((a) => <div key={a.step_id}>{a.step_id}: {a.approver} <span className="tag">{a.mode.replace("_", " ")}</span></div>) : "—"}</dd>
          </dl></div></section>
          <section className="card"><div className="card-head"><h2>Versions</h2><div className="row">
            <button className="btn sm" disabled={!canAct} onClick={() => setAct({ kind: "rollback" })}>Roll back</button></div></div>
            <ol className="timeline">{[...v.history].reverse().map((h, i) => <li key={i} className="tl-item" style={{ gridTemplateColumns: "1fr auto" }}>
              <div><div className="tl-title">v{h.version} <span className="tag">{h.action}</span>{h.version === v.current && <span className="pill ok" style={{ marginLeft: 8 }}>current</span>}</div><div className="tl-sub">{h.by} · <span title={when(h.at)}>{ago(h.at)}</span>{h.reason && ` · ${h.reason}`}</div></div>
              {h.version !== v.current && <button className="btn sm" disabled={!canAct} onClick={() => setAct({ kind: "promote", v: h.version })}>Make current</button>}</li>)}</ol></section>
        </div>
      </div>
      <section className="card"><div className="card-head"><h2>Compare versions</h2><div className="row">
        <select aria-label="From version" value={from} onChange={(e) => setFrom(e.target.value)} style={{ width: "auto" }}><option value="">From…</option>{v.versions.map((x) => <option key={x} value={x}>v{x}</option>)}</select>
        <span className="muted">→</span>
        <select aria-label="To version" value={to} onChange={(e) => setTo(e.target.value)} style={{ width: "auto" }}><option value="">To…</option>{v.versions.map((x) => <option key={x} value={x}>v{x}</option>)}</select></div></div>
        <div className="card-body">{!from || !to || from === to ? <p className="muted">Choose two different versions to see what changed between them.</p> : diff.error ? <ErrorState error={diff.error} /> : !diff.data ? <Skeleton lines={3} /> : <DiffView d={diff.data} />}</div></section>
      <ReasonDialog open={!!act} onClose={() => setAct(null)} title={act?.kind === "promote" ? `Make v${act.v} current` : "Roll back to the previous version"} confirm={act?.kind === "promote" ? "Make current" : "Roll back"}
        intro={<p>Production runs use the current version from the next run on. {act?.kind === "promote" ? "Only versions that pass validation can be promoted." : "This goes back to whichever version was current before the last promotion."}</p>} onSubmit={run} />
    </div>
  );
}
export default function Page() { return <Suspense fallback={<div className="page"><Skeleton lines={6} /></div>}><Artifact /></Suspense>; }
