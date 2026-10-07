"use client";
import Link from "next/link";
import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ApiError, api } from "@/lib/api";
import { useApi, useAuth, useToast } from "@/lib/hooks";
import { atLeast, type DiscoverSession, type DiscoverTurn } from "@/lib/types";
import { ago, label } from "@/lib/format";
import { Empty, ErrorState, PageHead, Skeleton } from "@/components/ui";
import { Alert, Lightbox, ReasonDialog, Shot } from "@/components/Panels";
import { DiscoverPill } from "@/components/Discover";
import { IconCheck, IconClock, IconSpinner, IconX } from "@/components/icons";

const ACTIVE = ["queued", "running", "awaiting_commit", "verifying"];

function Turns({ turns, active, onShot, id }: { turns: DiscoverTurn[]; active: boolean; onShot: (u: string, a: string) => void; id: string }) {
  if (turns.length === 0) return <div className="card-body muted small">{active ? "Signing on and opening the first page…" : "The model did not get to take a step."}</div>;
  return (
    <ol className="timeline" aria-label="What the model did">
      {turns.map((t, i) => {
        const last = i === turns.length - 1;
        const status = t.ok === null ? (active && last ? "running" : "not_run") : t.ok ? "ok" : "failed";
        return (
          <li key={t.n} className="tl-item" data-status={status === "running" ? "not_run" : status}>
            <span className="tl-dot" aria-hidden>{status === "ok" ? <IconCheck /> : status === "failed" ? <IconX /> : status === "running" ? <IconSpinner /> : <IconClock />}</span>
            <div>
              <div className="tl-title">{t.summary}{t.value ? <span className="muted"> → <strong>{t.value}</strong></span> : null}</div>
              <div className="tl-sub">{t.title ?? ""}{t.url ? <span className="mono"> {t.url.replace(/^https?:\/\/[^/]+/, "")}</span> : null}</div>
              {t.error && <div className="small" style={{ color: "var(--bad)" }}>{t.error}</div>}
              {t.commit && <div className="small">{t.commit.approved ? `Final step approved by ${t.commit.by ?? "the system"}.` : "Final step declined."}</div>}
            </div>
            {t.screenshot ? <Shot path={`/v1/discover/${id}/screenshots/${t.screenshot}`} alt={`Page before step ${t.n}`} onOpen={onShot} /> : <span />}
          </li>);
      })}
    </ol>
  );
}

function Session() {
  const id = useSearchParams().get("id") ?? "";
  const { me } = useAuth();
  const toast = useToast();
  const [every, setEvery] = useState(1500);
  const s = useApi<DiscoverSession>(id ? `/v1/discover/${id}` : null, { every });
  const [shot, setShot] = useState<{ url: string; alt: string } | null>(null);
  const [dlg, setDlg] = useState<null | "approve" | "decline" | "promote" | "discard">(null);
  const [busy, setBusy] = useState(false);
  const d = s.data;
  const settled = !!d && !ACTIVE.includes(d.status);
  useEffect(() => { if (settled) setEvery(0); }, [settled]);  // stops polling once nothing more can change
  if (!atLeast(me?.role, "supervisor")) return <div className="page"><Empty title="Discovery needs a supervisor" /></div>;
  if (!id) return <div className="page"><Empty title="No session chosen">Pick one from <Link href="/discover/">Discovery</Link>.</Empty></div>;
  if (s.error && !d) return <div className="page"><ErrorState error={s.error} retry={s.reload} /></div>;
  if (!d) return <div className="page"><Skeleton lines={6} /></div>;

  const turns = d.turns ?? [];
  const latest = [...turns].reverse().find((t) => t.screenshot);
  const active = ACTIVE.includes(d.status);
  const mine = d.created_by === me?.name;
  async function post(path: string, json: unknown, done: string) {
    setBusy(true);
    try { await api(`/v1/discover/${id}${path}`, { method: "POST", json }); toast(done); setEvery(1500); await s.reload(); }
    catch (e) { toast(e instanceof ApiError ? e.message : "Failed", true); throw e; } finally { setBusy(false); }
  }
  const v = d.verify;
  const read = Object.entries(v?.outputs ?? {}).filter(([k, x]) => k !== "status" && x !== null && x !== undefined);
  const lintErrors = (d.lint ?? []).filter((f) => f.level === "error");
  const lintWarnings = (d.lint ?? []).filter((f) => f.level === "warning");

  return (
    <div className="page">
      <PageHead title={d.name} crumb={<Link href="/discover/">← Discovery</Link>} sub={<><code>{d.capability_id}</code>{d.version ? ` · v${d.version}` : ""} · started by {d.created_by} {ago(d.created_at)} ago</>}>
        <DiscoverPill status={d.status} />
        {active && <button className="btn sm danger" disabled={busy} onClick={() => post("/cancel", {}, "Stopping…").catch(() => {})}>Stop</button>}
      </PageHead>

      {d.status === "running" && <Alert tone="info" icon={<IconSpinner width={20} height={20} />} title="The model is working on the practice system"><span>{turns.length} step{turns.length === 1 ? "" : "s"} so far. Nothing here can be run by anyone until you promote it.</span></Alert>}
      {d.status === "verifying" && <Alert tone="info" icon={<IconSpinner width={20} height={20} />} title="Checking the recorded task with the second set of values"><span>It is replaying exactly what it recorded, with no model involved.</span></Alert>}
      {d.status === "awaiting_commit" && d.commit_request && (
        <div className="banner wait" role="alert"><IconClock width={20} height={20} /><div className="grow">
          <strong>The model is about to take the final step</strong>
          <span>It wants to press <strong>{d.commit_request.description}</strong> on <span className="mono">{d.commit_request.url.replace(/^https?:\/\/[^/]+/, "")}</span> ({d.commit_request.page_title}). This is the practice system, but the step is really taken, and it becomes the recorded commit step of the new task.</span>
          <div className="row" style={{ marginTop: 6 }}>
            <button className="btn primary" disabled={busy} onClick={() => setDlg("approve")}>Let it take the step</button>
            <button className="btn danger" disabled={busy} onClick={() => setDlg("decline")}>Do not take it</button>
          </div></div></div>)}
      {(d.status === "stuck" || d.status === "failed" || d.status === "cancelled") && (
        <Alert tone={d.status === "failed" ? "bad" : "warn"} title={d.status === "cancelled" ? "Stopped" : d.status === "failed" ? "It could not finish" : "The model got stuck"}>
          <span>{d.error ?? "No reason was recorded."}{d.reasoning && d.status === "stuck" ? ` The model said: “${d.reasoning}”.` : ""}</span>
          <span className="small muted">Nothing was saved. {d.status === "stuck" ? "A hint about where it went wrong usually fixes this." : ""}</span>
          <div className="row" style={{ marginTop: 6 }}><Link className="btn primary" href={`/discover/new/?retry=${d.id}`}>Try again with a hint</Link></div>
        </Alert>)}
      {d.status === "needs_attention" && (
        <Alert tone="warn" title="Saved as a draft, but it is not ready">
          <span>{d.error}</span>
          {read.length > 0 && <span>It read: {read.map(([k, x]) => `${label(k)} = ${String(x)}`).join(" · ")}.</span>}
          {v?.ran && !v.passed && <span>Second set {v.inputs ? `(${Object.entries(v.inputs).map(([k, x]) => `${label(k)} ${String(x)}`).join(", ")})` : ""}: expected {v.expected}, got <strong>{v.business_outcome ?? v.status}</strong>{v.error ? ` (${v.error})` : ""}.</span>}
          {lintErrors.map((f, i) => <span key={i} className="small">• {f.message}</span>)}
          <div className="row" style={{ marginTop: 6 }}>
            <Link className="btn primary" href={`/discover/new/?retry=${d.id}`}>Try again with a hint</Link>
            {d.draft_url && <Link className="btn" href={`/artifact/?id=${encodeURIComponent(d.capability_id)}&version=${d.version}`}>Look at the recorded steps</Link>}
            <button className="btn danger" disabled={busy} onClick={() => setDlg("discard")}>Discard the draft</button>
          </div>
        </Alert>)}
      {d.status === "ready" && (
        <div className="banner ok" role="status"><IconCheck width={20} height={20} /><div className="grow">
          <strong>Recorded and checked. Nobody can run it until you promote it.</strong>
          <span>{d.steps} recorded step{d.steps === 1 ? "" : "s"}. {v?.ran ? <>Replayed with {Object.entries(v.inputs ?? {}).map(([k, x]) => `${label(k)} ${String(x)}`).join(", ")}: <strong>{v.business_outcome ? `the normal answer “${v.business_outcome}”` : "it worked"}</strong>.</> : "It was not run a second time, so it has only been seen working once."}{lintWarnings.length > 0 ? ` ${lintWarnings.length} note${lintWarnings.length === 1 ? "" : "s"} from the checker.` : ""}</span>
          {read.length > 0 && <span>It read <strong>{read.map(([k, x]) => `${label(k)} = ${String(x)}`).join(" · ")}</strong>. Check that against the system before you make it available.</span>}
          {v?.warnings?.map((w, i) => <span key={i} className="small">• {w}</span>)}
          {lintWarnings.map((f, i) => <span key={i} className="small">• {f.message}</span>)}
          <div className="row" style={{ marginTop: 6 }}>
            <Link className="btn" href={`/artifact/?id=${encodeURIComponent(d.capability_id)}&version=${d.version}`}>Review the recorded steps</Link>
            <button className="btn primary" disabled={busy} onClick={() => setDlg("promote")}>Make it available</button>
            <button className="btn danger" disabled={busy} onClick={() => setDlg("discard")}>Discard</button>
          </div></div></div>)}
      {d.status === "promoted" && <Alert tone="ok" title="This task is in use"><span>It is on the task list and can be run.</span><div className="row" style={{ marginTop: 6 }}><Link className="btn primary" href={`/task/?id=${encodeURIComponent(d.capability_id)}`}>Open the task</Link></div></Alert>}
      {d.status === "discarded" && <Alert tone="warn" title="Discarded"><span>The draft was removed. Nothing was published.</span></Alert>}

      <div className="split">
        <section className="card"><div className="card-head"><h2>What the model did</h2><span className="small muted">{turns.length} step{turns.length === 1 ? "" : "s"}</span></div>
          <Turns turns={turns} active={active} onShot={(u, a) => setShot({ url: u, alt: a })} id={d.id} /></section>
        <div className="stack">
          <section className="card"><div className="card-head"><h2>{active ? "What it sees now" : "The last page it saw"}</h2></div>
            <div className="card-body">{latest?.screenshot ? <Shot live path={`/v1/discover/${d.id}/screenshots/${latest.screenshot}`} alt="The page the model last looked at" onOpen={(u, a) => setShot({ url: u, alt: a })} /> : <p className="muted small">No picture yet.</p>}</div></section>
          {d.contract && (
            <section className="card"><div className="card-head"><h2>What you asked for</h2></div><div className="card-body"><dl className="kv small">
              <dt>System</dt><dd>{d.target}</dd><dt>Effect</dt><dd>{label(d.effect)}</dd>
              <dt>Goes in</dt><dd>{d.contract.inputs.map((i) => `${i.name} (e.g. ${i.example})`).join(", ") || "—"}</dd>
              <dt>Comes out</dt><dd>{d.contract.outputs.map((o) => o.name).join(", ") || "—"}</dd>
              <dt>Worked when</dt><dd>{d.contract.success_text ? <>page shows “{d.contract.success_text}”</> : "decided from the page it reads the answer from"}</dd></dl></div></section>)}
        </div>
      </div>

      {dlg === "approve" && <ReasonDialog open onClose={() => setDlg(null)} title="Let the model take the final step" confirm="Take the step"
        intro={<p>It is taken on the practice system and recorded as this task&apos;s commit step, with your name beside it.{mine ? "" : ` ${d.created_by} started this session.`}</p>}
        onSubmit={(reason) => post("/commit", { approve: true, reason }, "Approved. The model carries on.")} />}
      {dlg === "decline" && <ReasonDialog open onClose={() => setDlg(null)} title="Do not take the final step" confirm="Decline" danger
        intro={<p>Nothing is changed. The model is told no and will most likely give up, so you can try again with a hint.</p>}
        onSubmit={(reason) => post("/commit", { approve: false, reason }, "Declined.")} />}
      {dlg === "promote" && <ReasonDialog open onClose={() => setDlg(null)} title="Make this task available" confirm="Make it available"
        intro={<p>Operators will be able to run <strong>{d.name}</strong>. {d.effect === "irreversible" ? "It commits something, so every run will need approval first." : ""} You can roll it back from its page.</p>}
        onSubmit={(reason) => post("/promote", { reason }, "The task is available.")} />}
      {dlg === "discard" && <ReasonDialog open onClose={() => setDlg(null)} title="Discard this draft" confirm="Discard" danger
        intro={<p>The recorded draft is deleted. It was never available to run.</p>} onSubmit={(reason) => post("/discard", { reason }, "Discarded.")} />}
      <Lightbox shot={shot} onClose={() => setShot(null)} />
    </div>
  );
}
export default function Page() { return <Suspense fallback={<div className="page"><Skeleton lines={6} /></div>}><Session /></Suspense>; }
