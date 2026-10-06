"use client";
import { useEffect, useId, useState, type ReactNode } from "react";
import { ApiError, api, fetchBlobUrl } from "@/lib/api";
import { useAuth, useToast } from "@/lib/hooks";
import { atLeast, type RunView, type SignOn, type TimelineStep } from "@/lib/types";
import { IconAlert, IconCheck, IconClock, IconSkip, IconSpinner, IconX } from "./icons";
import { Dialog, Field } from "./ui";

/** Collects a reason, then runs an action. Every decision in the platform is recorded with who made it and why. */
export function ReasonDialog({ open, onClose, title, confirm, danger, onSubmit, extra, intro }: {
  open: boolean; onClose: () => void; title: string; confirm: string; danger?: boolean; intro?: ReactNode; extra?: ReactNode; onSubmit: (reason: string) => Promise<void>;
}) {
  const fieldId = useId();  // one per dialog: several are mounted at once on the inbox
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { if (open) { setReason(""); setErr(null); } }, [open]);
  async function go() {
    if (!reason.trim()) { setErr("A reason is required: it is recorded with your name."); return; }
    setBusy(true); setErr(null);
    try { await onSubmit(reason.trim()); onClose(); } catch (e) { setErr(e instanceof ApiError ? e.message : "Failed"); } finally { setBusy(false); }
  }
  return (
    <Dialog open={open} onClose={onClose} title={title} footer={<><button className="btn" onClick={onClose} disabled={busy}>Cancel</button><button className={`btn ${danger ? "danger" : "primary"}`} onClick={go} disabled={busy}>{busy ? "Working…" : confirm}</button></>}>
      {intro}{extra}
      <Field id={fieldId} label="Reason" error={err} hint="Recorded in the audit trail with your name.">
        <textarea id={fieldId} rows={3} value={reason} onChange={(e) => setReason(e.target.value)} aria-invalid={!!err} autoFocus />
      </Field>
    </Dialog>
  );
}

export function useDecide(onDone: () => void) {
  const toast = useToast();
  return async (path: string, json: unknown, ok: string) => {
    await api(path, { method: "POST", json });
    toast(ok); onDone();
  };
}

export function canDecideApproval(role: string | undefined, name: string | undefined, tier: string, requestedBy: string): { ok: boolean; why?: string } {
  if (name === requestedBy) return { ok: false, why: "You requested this, so someone else has to approve it." };
  const needed = tier === "supervisor" ? "supervisor" : "operator";
  if (!atLeast(role as never, needed)) return { ok: false, why: `This needs a ${needed}; your key is ${role}.` };
  return { ok: true };
}

export function ApprovalActions({ run, tier, onDone, compact }: { run: Pick<RunView, "id" | "requested_by">; tier: string; onDone: () => void; compact?: boolean }) {
  const { me } = useAuth();
  const decide = useDecide(onDone);
  const [mode, setMode] = useState<null | "approve" | "reject">(null);
  const gate = canDecideApproval(me?.role, me?.name, tier, run.requested_by);
  return (
    <>
      <div className="row">
        <button className={`btn ${compact ? "sm" : ""} primary`} disabled={!gate.ok} onClick={() => setMode("approve")} title={gate.why}>Approve and run</button>
        <button className={`btn ${compact ? "sm" : ""} danger`} disabled={!gate.ok} onClick={() => setMode("reject")} title={gate.why}>Reject</button>
        {!gate.ok && <span className="small muted">{gate.why}</span>}
      </div>
      <ReasonDialog open={mode === "approve"} onClose={() => setMode(null)} title="Approve this run" confirm="Approve and run"
        intro={<p>You are approving this for execution now. It will do exactly what the details on the page say.</p>}
        onSubmit={(reason) => decide(`/v1/runs/${run.id}/approve`, { reason, execute: true }, "Approved. The run has started.")} />
      <ReasonDialog open={mode === "reject"} onClose={() => setMode(null)} title="Reject this run" confirm="Reject" danger
        onSubmit={(reason) => decide(`/v1/runs/${run.id}/reject`, { reason }, "Rejected.")} />
    </>
  );
}

export function ResolveActions({ run, onDone, compact }: { run: Pick<RunView, "id" | "capability_id">; onDone: () => void; compact?: boolean }) {
  const { me } = useAuth();
  const decide = useDecide(onDone);
  const [open, setOpen] = useState(false);
  const [outcome, setOutcome] = useState<"committed" | "not_committed">("not_committed");
  const allowed = atLeast(me?.role, "operator");
  return (
    <>
      <button className={`btn ${compact ? "sm" : ""} primary`} disabled={!allowed} onClick={() => setOpen(true)}>Settle this run</button>
      <ReasonDialog open={open} onClose={() => setOpen(false)} title="Settle an unknown outcome" confirm="Record outcome"
        intro={<p>Check the target system's own records first. Whatever you record decides whether a retry is allowed: <strong>“did take effect”</strong> closes it for good; <strong>“did not take effect”</strong> frees it to be tried again.</p>}
        extra={<div className="chips" role="radiogroup" aria-label="Outcome">
          <button type="button" role="radio" aria-checked={outcome === "not_committed"} aria-pressed={outcome === "not_committed"} className="chip" onClick={() => setOutcome("not_committed")}>It did not take effect</button>
          <button type="button" role="radio" aria-checked={outcome === "committed"} aria-pressed={outcome === "committed"} className="chip" onClick={() => setOutcome("committed")}>It did take effect</button></div>}
        onSubmit={(reason) => decide(`/v1/runs/${run.id}/resolve`, { outcome, reason }, "Outcome recorded.")} />
    </>
  );
}

/** A screenshot from a run. It needs the API key, so it is fetched and shown from an object URL. */
export function Shot({ path, alt, onOpen, big, live }: { path: string; alt: string; onOpen?: (url: string, alt: string) => void; big?: boolean; live?: boolean }) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    // the previous picture stays on screen until the next one has loaded, so a live view updates without flickering
    let alive = true;
    fetchBlobUrl(path).then((u) => { if (!alive) { URL.revokeObjectURL(u); return; } setFailed(false); setUrl((old) => { if (old) URL.revokeObjectURL(old); return u; }); }).catch(() => alive && setFailed(true));
    return () => { alive = false; };
  }, [path]);
  useEffect(() => () => { setUrl((old) => { if (old) URL.revokeObjectURL(old); return null; }); }, []);
  if (failed) return <span className="small muted">no image</span>;
  if (!url) return <div className={`skeleton thumb ${live ? "live" : big ? "big" : ""}`} aria-hidden />;
  // eslint-disable-next-line @next/next/no-img-element
  return <img className={`thumb ${live ? "live" : big ? "big" : ""}`} src={url} alt={alt} loading="lazy" onClick={() => onOpen?.(url, alt)} tabIndex={0} onKeyDown={(e) => { if (e.key === "Enter") onOpen?.(url, alt); }} />;
}

const ICON = { ok: <IconCheck />, failed: <IconX />, skipped: <IconSkip />, not_run: <IconClock />, running: <IconSpinner /> };

export function StepTimeline({ steps, runId, running, onShot, signOn }: { steps: TimelineStep[]; runId: string; running: boolean; onShot: (url: string, alt: string) => void; signOn?: SignOn | null }) {
  const firstPending = running ? steps.findIndex((s) => s.status === "not_run") : -1;
  return (
    <ol className="timeline">
      {signOn && (
        <li className="tl-item" data-status="failed">
          <span className="tl-dot" aria-hidden><IconX /></span>
          <div>
            <div className="tl-title">Sign in to the system</div>
            <div className="tl-sub">Failed before the task could start{signOn.step_id ? ` (at its step ${signOn.step_id})` : ""}</div>
            <dl className="expect"><dt>Actual</dt><dd style={{ color: "var(--bad)" }}>{signOn.actual ?? "sign-on did not work"}</dd></dl>
          </div>
          {signOn.screenshot ? <Shot path={`/v1/runs/${runId}/screenshots/${signOn.screenshot}`} alt="Page where sign-on failed" onOpen={onShot} /> : <span />}
        </li>
      )}
      {steps.map((s, i) => {
        const live = i === firstPending;
        const status = live ? "running" : s.status;
        return (
          <li key={s.step_id} className="tl-item" data-status={s.status}>
            <span className="tl-dot" aria-hidden>{ICON[status as keyof typeof ICON]}</span>
            <div>
              <div className="tl-title">{s.description}{s.risk === "irreversible" && <span className="tag danger" style={{ marginLeft: 8 }}>commits</span>}{s.optional_input && <span className="tag" style={{ marginLeft: 8 }}>only if “{s.optional_input.replace(/_/g, " ")}” given</span>}</div>
              <div className="tl-sub">
                <span className="sr-only">Step {s.n}: </span>{s.status === "ok" ? "Done" : s.status === "failed" ? "Failed here" : s.status === "skipped" ? "Skipped, because its input was not supplied" : live ? "In progress…" : "Not reached"}
              </div>
              {s.status === "failed" && (
                <dl className="expect">
                  {s.expected && <><dt>Expected</dt><dd>{s.expected}</dd></>}
                  <dt>Actual</dt><dd style={{ color: "var(--bad)" }}>{s.actual ?? "it did not work"}{s.error_code && <span className="tag" style={{ marginLeft: 8 }}>{s.error_code}</span>}</dd>
                </dl>
              )}
            </div>
            {s.screenshot ? <Shot path={`/v1/runs/${runId}/screenshots/${s.screenshot}`} alt={`Page after step ${s.n}`} onOpen={onShot} /> : <span />}
          </li>
        );
      })}
    </ol>
  );
}

export function Lightbox({ shot, onClose }: { shot: { url: string; alt: string } | null; onClose: () => void }) {
  return (
    <Dialog open={!!shot} onClose={onClose} title={shot?.alt ?? ""} footer={<button className="btn" onClick={onClose}>Close</button>}>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      {shot && <div className="lightbox"><img src={shot.url} alt={shot.alt} /></div>}
    </Dialog>
  );
}

export function Alert({ tone, icon, title, children }: { tone: "info" | "warn" | "bad" | "wait" | "ok"; icon?: ReactNode; title: string; children?: ReactNode }) {
  const def = tone === "ok" ? <IconCheck width={20} height={20} /> : tone === "bad" ? <IconX width={20} height={20} /> : tone === "warn" ? <IconAlert width={20} height={20} /> : <IconClock width={20} height={20} />;
  return <div className={`banner ${tone}`} role={tone === "bad" ? "alert" : "status"}>{icon ?? def}<div className="grow"><strong>{title}</strong>{children}</div></div>;
}
