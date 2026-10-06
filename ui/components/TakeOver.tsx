"use client";
import { useState } from "react";
import { ApiError, api } from "@/lib/api";
import { useApi, useToast } from "@/lib/hooks";
import type { EscalationElement, EscalationState } from "@/lib/types";
import { Alert, Shot } from "./Panels";
import { Dialog } from "./ui";
import { IconAlert, IconCheck, IconX } from "./icons";

const CLICKABLE = new Set(["button", "link", "checkbox", "radio", "tab", "menuitem", "option", "switch"]);
const TYPEABLE = new Set(["textbox", "searchbox", "spinbutton"]);

function ElementRow({ el, runId, onAct, busy }: { el: EscalationElement; runId: string; onAct: (kind: "click" | "type" | "select", ref: string, value?: string) => Promise<void>; busy: boolean }) {
  const [value, setValue] = useState(el.value ?? "");
  const name = el.name || `unnamed ${el.role}`;
  const kind = TYPEABLE.has(el.role) ? "type" : el.role === "combobox" ? "select" : CLICKABLE.has(el.role) ? "click" : null;
  if (!kind) return null;
  const id = `${runId}-${el.ref}`;
  return (
    <li className="row" style={{ alignItems: "center", gap: 8, padding: "6px 0", borderBottom: "1px solid var(--border)" }}>
      <span className="tag">{el.role}</span>
      <label htmlFor={id} style={{ minWidth: 130, flex: "0 1 auto" }}><strong>{name}</strong></label>
      {kind === "type" && <><input id={id} value={value} disabled={el.disabled} onChange={(e) => setValue(e.target.value)} style={{ flex: 1, minWidth: 120 }} aria-label={`Text for ${name}`} />
        <button className="btn sm" disabled={busy || el.disabled} onClick={() => onAct("type", el.ref, value)} aria-label={`Type into ${name}`}>Type it</button></>}
      {kind === "select" && <><select id={id} value={value} disabled={el.disabled} onChange={(e) => setValue(e.target.value)} style={{ flex: 1, minWidth: 120 }} aria-label={`Choice for ${name}`}>
        {!(el.options ?? []).includes(value) && <option value={value}>{value || "—"}</option>}{(el.options ?? []).map((o) => <option key={o} value={o}>{o}</option>)}</select>
        <button className="btn sm" disabled={busy || el.disabled || !value} onClick={() => onAct("select", el.ref, value)} aria-label={`Choose in ${name}`}>Choose it</button></>}
      {kind === "click" && <><span className="grow" style={{ flex: 1 }} /><button id={id} className="btn sm" disabled={busy || el.disabled} onClick={() => onAct("click", el.ref)} aria-label={`Click ${name}`}>Click</button></>}
    </li>
  );
}

/** A run that got stuck is waiting for a person: see the page it is on, act on it (the run's own browser session, not a copy), then hand it back or stop it. */
export function TakeOver({ runId, onChange, onShot }: { runId: string; onChange: () => void; onShot: (url: string, alt: string) => void }) {
  const toast = useToast();
  const st = useApi<EscalationState>(`/v1/runs/${runId}/escalation`, { every: 1500 });
  const [busy, setBusy] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);
  const d = st.data;
  if (!d || !d.paused) return null;
  const p = d.paused;

  async function call(path: string, json?: unknown) {
    setBusy(true);
    try { await api(`/v1/runs/${runId}/escalation/${path}`, { method: "POST", json }); }
    catch (e) { toast(e instanceof ApiError ? e.message : "Failed", true); throw e; }
    finally { setBusy(false); }
  }
  const act = async (kind: "click" | "type" | "select", ref: string, value?: string) => { try { await call("act", { kind, ref, value }); } catch { /* shown */ } setTimeout(() => void st.reload(), 700); };
  const resume = async () => { try { await call("resume"); toast("Handed back. The run carries on."); onChange(); } catch { /* shown */ } };
  const stop = async () => { try { await call("stop"); toast("The run was stopped."); setConfirmStop(false); onChange(); } catch { /* shown */ } };
  const mins = p.stops_in_s === null ? null : Math.max(1, Math.ceil(p.stops_in_s / 60));

  return (
    <section className="card" aria-label="Take over" style={{ borderColor: "var(--wait)" }}>
      <div className="card-head"><h2>This run needs a person</h2>{mins !== null && <span className="small muted">stops by itself in about {mins} min if nobody helps</span>}</div>
      <div className="card-body stack" style={{ gap: 14 }}>
        <Alert tone="wait" icon={<IconAlert width={20} height={20} />} title={p.step_id ? `Stuck at step ${p.step_id}` : "Stuck at the end"}>
          <span>{p.reason}</span>
          <span className="small muted">You are working on the run&apos;s own browser, the page below. Fix what is in the way, then hand it back: it checks the step is done before it goes on, so nothing you did is repeated.</span>
        </Alert>
        <div className="grid-2">
          <div className="stack" style={{ gap: 6 }}>
            <div className="label">The page right now{d.title ? ` · ${d.title}` : ""}</div>
            {d.has_screenshot ? <Shot live path={`/v1/runs/${runId}/escalation/screenshot?t=${d.screenshot_token}`} alt="The page the run is stuck on" onOpen={onShot} /> : <div className="small muted">No picture yet.</div>}
            {d.url && <code className="small" style={{ overflowWrap: "anywhere" }}>{d.url.replace(/^https?:\/\/[^/]+/, "")}</code>}
          </div>
          <div className="stack" style={{ gap: 6 }}>
            <div className="label">What you can do on it</div>
            <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>{d.elements.map((el) => <ElementRow key={el.ref + el.role} el={el} runId={runId} onAct={act} busy={busy} />)}</ul>
            {d.elements.length === 0 && <div className="small muted">Nothing to click or type on this page.</div>}
            {d.last_action && <div className="small" role="status" style={{ color: d.last_action.ok ? "var(--ok)" : "var(--bad)" }}>{d.last_action.ok ? <><IconCheck width={14} height={14} /> That worked.</> : <><IconX width={14} height={14} /> That did not work: {d.last_action.error}</>}</div>}
          </div>
        </div>
        <div className="row">
          <button className="btn primary" disabled={busy} onClick={resume}>Hand it back</button>
          <button className="btn danger" disabled={busy} onClick={() => setConfirmStop(true)}>Stop the run</button>
        </div>
      </div>
      <Dialog open={confirmStop} onClose={() => setConfirmStop(false)} title="Stop this run?" footer={<><button className="btn" onClick={() => setConfirmStop(false)}>Keep waiting</button><button className="btn danger" disabled={busy} onClick={stop}>Stop the run</button></>}>
        <p>It ends as a failure and nothing more is done. If it already changed something, check the system before running it again.</p>
      </Dialog>
    </section>
  );
}
