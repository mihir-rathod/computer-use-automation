"use client";
import Link from "next/link";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ApiError, api, download, streamEvents } from "@/lib/api";
import { useApi, useAuth, useToast } from "@/lib/hooks";
import { atLeast, type RunView, type Timeline } from "@/lib/types";
import { ago, duration, formatValue, label, when } from "@/lib/format";
import { Empty, ErrorState, PageHead, Skeleton, StatusPill, isActive, isSettled } from "@/components/ui";
import { Alert, ApprovalActions, Lightbox, ResolveActions, StepTimeline } from "@/components/Panels";
import { TakeOver } from "@/components/TakeOver";
import { LiveView } from "@/components/LiveView";
import { IconClock, IconInbox } from "@/components/icons";

function Run() {
  const id = useSearchParams().get("id") ?? "";
  const { me } = useAuth();
  const toast = useToast();
  const [live, setLive] = useState<string[]>([]);
  const [shot, setShot] = useState<{ url: string; alt: string } | null>(null);
  const run = useApi<RunView>(id ? `/v1/runs/${id}` : null);
  const tl = useApi<Timeline>(id ? `/v1/runs/${id}/timeline` : null);
  const status = run.data?.status ?? "";
  const active = isActive(status);
  const refresh = useCallback(() => { void run.reload(); void tl.reload(); }, [run.reload, tl.reload]); // eslint-disable-line react-hooks/exhaustive-deps

  // Live progress while the run is active: an SSE stream for log lines and status changes, with a slow poll as a safety net.
  useEffect(() => {
    if (!id || !active) return;
    const ctl = new AbortController();
    const summarize = (d: Record<string, unknown>) => {
      const data = (d.data ?? {}) as Record<string, unknown>;
      const t = String(d.event_type);
      if (t === "action") return `${data.action_kind ?? "action"}${data.resolved_target ? ` · ${(data.resolved_target as { semantic_description?: string }).semantic_description ?? ""}` : ""}${data.success === false ? " — failed" : ""}`;
      if (t === "step") return `step ${data.step_id} ${data.status}`;
      return t.replace(/_/g, " ");
    };
    streamEvents(`/v1/runs/${id}/events`, (e) => {
      if (e.event === "log") setLive((l) => [...l.slice(-199), summarize(e.data as Record<string, unknown>)]);
      if (e.event === "status" || e.event === "done") refresh();
      if (e.event === "log" && String((e.data as { event_type?: string }).event_type) === "step") void tl.reload();
    }, ctl.signal).catch(() => { /* aborted or dropped: the poll below covers it */ });
    const t = setInterval(refresh, run.data?.pace_ms ? 1000 : 3000);
    return () => { ctl.abort(); clearInterval(t); };
  }, [id, active, run.data?.pace_ms]); // eslint-disable-line react-hooks/exhaustive-deps
  const logRef = useRef<HTMLDivElement>(null);
  useEffect(() => { logRef.current?.scrollTo({ top: logRef.current.scrollHeight }); }, [live]);

  if (!id) return <div className="page"><Empty title="No run chosen">Pick one from <Link href="/runs/">Runs</Link>.</Empty></div>;
  if (run.error && !run.data) return <div className="page"><ErrorState error={run.error} retry={run.reload} /></div>;
  if (!run.data) return <div className="page"><Skeleton lines={6} /></div>;
  const r = run.data, res = r.result, steps = tl.data?.steps ?? [];
  const pending = r.approvals?.find((a) => a.decision === null);
  const done = steps.filter((s) => s.status === "ok").length;

  async function cancel() {
    try { await api(`/v1/runs/${id}/cancel`, { method: "POST" }); toast("Cancelling after the current action."); } catch (e) { toast(e instanceof ApiError ? e.message : "Could not cancel", true); }
  }

  return (
    <div className="page">
      <PageHead title={r.capability_id} crumb={<Link href="/runs/">← Runs</Link>} sub={<>Run <span className="mono">{r.id}</span> · requested by <strong>{r.requested_by}</strong> {ago(r.created_at)}{r.version && ` · artifact v${r.version}`}</>}>
        <StatusPill status={r.status} paused={!!r.paused} />
        {(r.status === "queued" || r.status === "running") && atLeast(me?.role, "operator") && <button className="btn sm danger" onClick={cancel}>Cancel run</button>}
      </PageHead>

      {r.status === "pending_approval" && pending && (
        <Alert tone="wait" icon={<IconClock width={20} height={20} />} title={`Waiting for a ${pending.tier} to approve`}>
          <span>Nothing has been done yet. Review what is being asked below, then approve or reject.</span>
          <div style={{ marginTop: 8 }}><ApprovalActions run={r} tier={pending.tier} onDone={refresh} /></div>
        </Alert>
      )}
      {r.status === "rejected" && <Alert tone="info" title="Rejected">{r.approvals?.filter((a) => a.decision === "rejected").map((a) => <span key={a.id}>{a.decided_by} rejected it: “{a.reason}”</span>)}</Alert>}
      {r.status === "needs_review" && (
        <Alert tone="warn" title={r.resolution ? "Outcome settled by a person" : "The outcome is unknown"}>
          {r.resolution ? <span>It was recorded as <strong>{r.resolution === "committed" ? "having taken effect" : "not having taken effect"}</strong>.</span>
            : <><span>The final step was issued but could not be confirmed, so it was <strong>not retried</strong>. Check the target system, then record what you found.</span>
              <div style={{ marginTop: 8 }}><ResolveActions run={r} onDone={refresh} /></div></>}
        </Alert>
      )}
      {r.status === "hard_failure" && res?.error && (
        <Alert tone="bad" title="This run failed"><span>{res.error.message}</span>
          <span className="small muted">Code: <code>{res.error.code ?? "unknown"}</code>{res.error.step_id && ` · at step ${res.error.step_id}`}{r.committed ? " · it had already committed" : ""}</span>
          {(r.repairs?.length ?? 0) > 0 && <span><Link href="/inbox/"><IconInbox width={14} height={14} style={{ verticalAlign: "-2px" }} /> A repair was proposed. Review it in the inbox.</Link></span>}</Alert>
      )}
      {r.status === "business_outcome" && res && <Alert tone="info" title={`Answered: ${label(res.business_outcome ?? "")}`}><span>This is a normal answer from the system, not an error.</span></Alert>}
      {r.status === "dry_run" && <Alert tone="info" title="Rehearsal only"><span>It stopped before the irreversible step. Nothing was changed.</span></Alert>}
      {r.paused && atLeast(me?.role, "operator") && <TakeOver runId={r.id} onChange={refresh} onShot={(url, alt) => setShot({ url, alt })} />}
      {r.paused && !atLeast(me?.role, "operator") && <Alert tone="wait" title="This run needs a person"><span>{r.paused.reason}. An operator can take over from this page.</span></Alert>}
      {active && !r.paused && <Alert tone="info" title={r.status === "queued" ? "Waiting for a browser to be free" : `Running · ${done} of ${steps.length || "?"} steps done`}><span className="small">This page updates by itself.{r.pace_ms > 0 && ` Watch mode is on (${r.pace_ms} ms around each action).`}{r.show_window && " A browser window is open on the machine running the server."}</span></Alert>}
      {active && r.status === "running" && atLeast(me?.role, "operator") && (r.pace_ms > 0 || (tl.data?.screenshots.length ?? 0) > 0) && (
        <section className="card" aria-label="Live view"><div className="card-head"><h2>Live view</h2><span className="small muted">outlined in amber: what it is touching</span></div>
          <div className="card-body"><LiveView runId={r.id} active big onOpen={(url, alt) => setShot({ url, alt })} /></div></section>
      )}

      <div className="split">
        <div className="stack" style={{ gap: 16 }}>
          {res?.outputs && Object.values(res.outputs).some((v) => v !== null) && (
            <section className="card"><div className="card-head"><h2>Result</h2></div><div className="card-body"><dl className="kv">{Object.entries(res.outputs).filter(([, v]) => v !== null).map(([k, v]) => <div key={k} style={{ display: "contents" }}><dt>{label(k)}</dt><dd>{formatValue(v)}</dd></div>)}</dl></div></section>
          )}
          <section className="card"><div className="card-head"><h2>Steps</h2><span className="small muted">{steps.length ? `${done} of ${steps.length} done` : ""}</span></div>
            {tl.error && !tl.data ? <div className="card-body"><ErrorState error={tl.error} retry={tl.reload} /></div> : !tl.data ? <div className="card-body"><Skeleton lines={4} /></div>
              : steps.length === 0 ? <Empty title="No steps to show">This run was settled before it started, so there is nothing to replay.</Empty>
              : <StepTimeline steps={steps} signOn={tl.data?.sign_on} runId={r.id} running={active} onShot={(url, alt) => setShot({ url, alt })} />}
            {tl.data?.success_expectation && <div className="card-body small muted" style={{ borderTop: "1px solid var(--border)" }}>The run counts as successful when {tl.data.success_expectation}.</div>}
          </section>
          {(active || live.length > 0) && <section className="card"><div className="card-head"><h2>Live activity</h2></div><div className="card-body"><div className="logbox" ref={logRef} tabIndex={0} aria-label="Live activity log">{live.length ? live.join("\n") : "Waiting for the first step…"}</div></div></section>}
        </div>

        <div className="stack" style={{ gap: 16 }}>
          <section className="card"><div className="card-head"><h2>Details</h2></div><div className="card-body"><dl className="kv small">
            <dt>Started</dt><dd>{when(r.started_at ?? r.created_at)}</dd><dt>Took</dt><dd>{isSettled(r.status) ? duration(r.started_at, r.finished_at) : "…"}</dd>
            <dt>Signed in as</dt><dd>{r.target ?? "—"}</dd>
            <dt>Committed</dt><dd>{r.committed ? <strong>Yes, at step {r.commit_step}</strong> : "No"}</dd>
            {res?.escalated && <><dt>Escalated</dt><dd>A person stepped in</dd></>}{res?.recovered && <><dt>Recovered</dt><dd>A known hiccup was handled automatically</dd></>}
            {res?.deduplicated && <><dt>Duplicate</dt><dd>Answered from an earlier run with the same key</dd></>}
            {r.idempotency_key && <><dt>Key</dt><dd className="mono">{r.idempotency_key}</dd></>}
          </dl></div></section>
          <section className="card"><div className="card-head"><h2>What was asked</h2></div><div className="card-body"><dl className="kv small">
            {Object.keys(r.params).length === 0 ? <dd>No inputs</dd> : Object.entries(r.params).map(([k, v]) => <div key={k} style={{ display: "contents" }}><dt>{label(k)}</dt><dd>{formatValue(v)}</dd></div>)}
          </dl></div></section>
          {(r.approvals?.length ?? 0) > 0 && (
            <section className="card"><div className="card-head"><h2>Approvals</h2></div><div className="card-body stack" style={{ gap: 10 }}>
              {r.approvals!.map((a) => <div key={a.id} className="small"><strong>{a.tier === "resolution" ? "Settled" : a.decision ?? "Waiting"}</strong>{a.decided_by ? ` by ${a.decided_by}` : ` for a ${a.tier}`}{a.decided_at && <span className="muted"> · {ago(a.decided_at)}</span>}{a.reason && <div className="muted">“{a.reason}”</div>}</div>)}
            </div></section>
          )}
          {r.has_trace && atLeast(me?.role, "operator") && (
            <section className="card card-pad stack" style={{ gap: 8 }}><strong>Debug trace</strong><span className="small muted">A recording of the browser for this failed run: open it with <code>playwright show-trace</code>. It contains whatever was typed.</span>
              <button className="btn sm" onClick={() => download(`/v1/runs/${r.id}/trace`, `${r.id}-trace.zip`).catch(() => toast("Could not download the trace", true))}>Download trace</button></section>
          )}
        </div>
      </div>
      <Lightbox shot={shot} onClose={() => setShot(null)} />
    </div>
  );
}
export default function Page() { return <Suspense fallback={<div className="page"><Skeleton lines={6} /></div>}><Run /></Suspense>; }
