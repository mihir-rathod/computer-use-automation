"use client";
import { useState } from "react";
import { ApiError, api } from "@/lib/api";
import { useApi, useAuth, useToast } from "@/lib/hooks";
import type { ApprovalState, AwaitingApproval } from "@/lib/types";
import { formatValue, label } from "@/lib/format";
import { Alert, ReasonDialog, Shot, canDecideApproval, useDecide } from "./Panels";
import { Dialog } from "./ui";
import { ElementRow } from "./TakeOver";
import { IconAlert, IconCheck, IconClock, IconX } from "./icons";

/** A run stopped just before something it cannot undo. The page is the run's own, exactly as it will be when the step goes ahead: read it, operate it if it needs
 *  touching, then approve (the run does the step), do the step yourself and hand it back, or reject. */
export function StepApproval({ runId, ask, onChange, onShot }: { runId: string; ask: AwaitingApproval; onChange: () => void; onShot: (url: string, alt: string) => void }) {
  const { me } = useAuth();
  const toast = useToast();
  const st = useApi<ApprovalState>(`/v1/runs/${runId}/approval`, { every: 1500 });
  const decide = useDecide(onChange);
  const [mode, setMode] = useState<null | "approve" | "reject" | "done">(null);
  const [busy, setBusy] = useState(false);
  const [leaving, setLeaving] = useState<string | null>(null); // a link the person pressed, waiting for them to confirm they mean to leave the page
  const d = st.data;
  const gate = canDecideApproval(me?.role, me?.name, ask.tier, ask.requested_by);
  const mins = ask.stops_in_s === null ? null : Math.max(1, Math.ceil(ask.stops_in_s / 60));
  const params = Object.entries(d?.params ?? {});

  async function resume() {
    setBusy(true);
    try { await api(`/v1/runs/${runId}/approval/resume`, { method: "POST", json: {} }); toast("Resuming: the run goes back through its earlier steps and asks again."); onChange(); setTimeout(() => void st.reload(), 1000); }
    catch (e) { toast(e instanceof ApiError ? e.message : "Failed", true); }
    finally { setBusy(false); }
  }

  async function act(kind: "click" | "type" | "select", ref: string, value?: string) {
    setBusy(true);
    try { await api(`/v1/runs/${runId}/approval/act`, { method: "POST", json: { kind, ref, value } }); }
    catch (e) { toast(e instanceof ApiError ? e.message : "Failed", true); }
    finally { setBusy(false); }
    setTimeout(() => void st.reload(), 700);
  }

  // A link takes the run's browser to another page, and the step's own button is not on that one: ask first.
  const onAct = async (kind: "click" | "type" | "select", ref: string, value?: string) => {
    if (kind === "click" && d?.elements?.find((e) => e.ref === ref)?.role === "link") { setLeaving(ref); return; }
    await act(kind, ref, value);
  };
  const moved = !!d?.page_moved;
  const working = !!d && !d.awaiting; // the run is going back through its earlier steps (after Resume automation) and has not asked again yet: nothing to decide for a moment

  return (
    <section className="card" aria-label="Approval needed" style={{ borderColor: "var(--wait)" }}>
      <div className="card-head"><h2>Waiting for your approval</h2>{mins !== null && <span className="small muted">stops by itself in about {mins} min if nobody decides</span>}</div>
      <div className="card-body stack" style={{ gap: 14 }}>
        <Alert tone="wait" icon={<IconClock width={20} height={20} />} title={`Needs a ${ask.tier}: ${ask.description ?? "the last step"}`}>
          <span>The run stopped just before this step, which cannot be undone. Nothing has been committed by the run. Check the page and the values below, then approve, reject, or work on the page yourself.</span>
          <span className="small muted">Asked by <strong>{ask.requested_by}</strong>.{me?.role === "admin" ? " As an admin you may decide your own request." : " Whoever asked cannot approve it."}</span>
        </Alert>
        {working && <Alert tone="info" title="The run is going back through its earlier steps"><span>This panel comes back as soon as it asks again. Nothing is committed on the way.</span></Alert>}
        {moved && !working && <Alert tone="bad" icon={<IconAlert width={20} height={20} />} title="You are no longer on the page the run stopped on">
          <span>The button for <strong>{ask.description ?? "the last step"}</strong> is not on this page, so approving cannot work from here. Press <strong>Resume automation</strong>: the run goes back through its earlier steps to the page it stopped on, and asks you again. Nothing is committed on the way.</span>
        </Alert>}
        <div className="grid-2">
          <div className="stack" style={{ gap: 6 }}>
            <div className="label">The page it is stopped on{d?.title ? ` · ${d.title}` : ""}</div>
            {d?.has_screenshot ? <Shot live path={`/v1/runs/${runId}/approval/screenshot?t=${d.screenshot_token}`} alt="The page the run is stopped on" onOpen={onShot} /> : <div className="small muted">No picture yet.</div>}
            {params.length > 0 && <div className="stack" style={{ gap: 6 }}><div className="label">What was asked</div>
              <dl className="kv small">{params.map(([k, v]) => <div key={k} style={{ display: "contents" }}><dt>{label(k)}</dt><dd>{formatValue(v)}</dd></div>)}</dl></div>}
            {d?.page_text && <div className="stack" style={{ gap: 6 }}><div className="label">What the page says</div><div className="logbox" tabIndex={0} aria-label="What the page says" style={{ maxHeight: 200 }}>{d.page_text}</div></div>}
          </div>
          <div className="stack" style={{ gap: 6 }}>
            <div className="label">What you can do on the page</div>
            <p className="small muted" style={{ margin: 0 }}>You are working on the run&apos;s own browser. Anything you click is done for real, including the final step. If you do the step yourself, press <strong>I did it myself</strong> so the run carries on.</p>
            <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>{(d?.elements ?? []).map((el) => <ElementRow key={el.ref + el.role} el={el} runId={runId} onAct={onAct} busy={busy || !gate.ok || working} />)}</ul>
            {(d?.elements?.length ?? 0) === 0 && <div className="small muted">Nothing to click or type on this page.</div>}
            {d?.last_action && <div className="small" role="status" style={{ color: d.last_action.ok ? "var(--ok)" : "var(--bad)" }}>{d.last_action.ok ? <><IconCheck width={14} height={14} /> That worked.</> : <><IconX width={14} height={14} /> {d.last_action.kind === "note" ? d.last_action.error : `That did not work: ${d.last_action.error}`}</>}</div>}
          </div>
        </div>
        <div className="row">
          <button className="btn primary" disabled={!gate.ok || moved || working} onClick={() => setMode("approve")} title={moved ? "The page has changed: the step's button is not on it any more." : gate.why}>Approve</button>
          <button className={`btn ${moved ? "primary" : ""}`} disabled={!gate.ok || busy || working} onClick={resume} title={gate.why ?? "The run goes back through its earlier steps to this page and asks you again"}>Resume automation</button>
          <button className="btn" disabled={!gate.ok || working} onClick={() => setMode("done")} title={gate.why}>I did it myself</button>
          <button className="btn danger" disabled={!gate.ok || working} onClick={() => setMode("reject")} title={gate.why}>Reject</button>
          {!gate.ok && <span className="small muted">{gate.why}</span>}
        </div>
      </div>
      <Dialog open={leaving !== null} onClose={() => setLeaving(null)} title="Leave the page the run stopped on?"
        footer={<><button className="btn" onClick={() => setLeaving(null)}>Stay here</button><button className="btn danger" onClick={() => { const ref = leaving; setLeaving(null); if (ref) void act("click", ref); }}>Leave the page</button></>}>
        <p>This link takes the run&apos;s browser away from <strong>{ask.description ?? "the step it stopped at"}</strong>. The run cannot carry on from another page, but <strong>Resume automation</strong> brings it back: it goes through its earlier steps again and asks you again.</p>
      </Dialog>
      <ReasonDialog open={mode === "approve"} onClose={() => setMode(null)} title="Approve this step" confirm="Approve it"
        intro={<p>The run goes ahead and does <strong>{ask.description ?? "the last step"}</strong> as soon as you approve, on the page as it is now. It cannot be undone.</p>}
        onSubmit={(reason) => decide(`/v1/runs/${runId}/approval/approve`, { reason }, "Approved. The run carries on.")} />
      <ReasonDialog open={mode === "done"} onClose={() => setMode(null)} title="Hand the run back" confirm="I did it"
        intro={<p>You did <strong>{ask.description ?? "the last step"}</strong> yourself on the page. The run checks that the page confirms it, then carries on without doing it again.</p>}
        onSubmit={(reason) => decide(`/v1/runs/${runId}/approval/done`, { reason }, "Handed back. The run carries on.")} />
      <ReasonDialog open={mode === "reject"} onClose={() => setMode(null)} title="Reject this step" confirm="Reject" danger
        intro={<p>The run stops here and does not commit anything. If you already changed something on the page yourself, check the system.</p>}
        onSubmit={(reason) => decide(`/v1/runs/${runId}/approval/reject`, { reason }, "Rejected. The run did not commit it.")} />
    </section>
  );
}
