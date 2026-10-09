"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { ApiError, api } from "@/lib/api";
import { useApi, useAuth, useToast } from "@/lib/hooks";
import { useWatchPref, watchBody } from "@/lib/watch";
import { WatchControl } from "@/components/WatchControl";
import { LiveView } from "@/components/LiveView";
import { atLeast, type Capability, type Features, type RunView } from "@/lib/types";
import { formatValue, label } from "@/lib/format";
import { Dialog, Empty, ErrorState, PageHead, Skeleton, StatusPill, isActive } from "@/components/ui";
import { Alert, ApprovalActions, ResolveActions, canDecideApproval } from "@/components/Panels";
import { IconAlert, IconChat, IconClock } from "@/components/icons";

interface Msg { id: number; role: "user" | "assistant"; text: string; run_id: string | null; created_at: string }

function RunCard({ runId }: { runId: string }) {
  const { me } = useAuth();
  const [every, setEvery] = useState<number | undefined>(2000);
  const run = useApi<RunView>(`/v1/runs/${runId}`, { every });
  const r = run.data;
  useEffect(() => { setEvery(r && !isActive(r.status) && r.status !== "pending_approval" ? undefined : r?.status === "pending_approval" ? 5000 : 2000); }, [r?.status]); // eslint-disable-line react-hooks/exhaustive-deps
  if (run.error && !r) return <div className="small muted">Couldn't load this run. <button className="btn sm ghost" onClick={run.reload}>Retry</button></div>;
  if (!r) return <div className="skeleton" style={{ height: 64, width: "100%" }} aria-busy />;
  const res = r.result;
  const pending = r.approvals?.find((a) => a.decision === null);
  const outputs = res?.outputs ? Object.entries(res.outputs).filter(([, v]) => v !== null) : [];
  return (
    <div className="card" style={{ padding: 12, marginTop: 8, width: "100%" }} data-testid="run-card">
      <div className="row"><StatusPill status={r.status} paused={!!r.paused} awaiting={!!r.awaiting_approval} /><span className="small muted mono">{r.id.replace("run_", "").slice(0, 20)}</span><span className="spacer" /><Link className="small" href={`/run/?id=${r.id}`}>Open run →</Link></div>
      {r.paused && (
        <div style={{ marginTop: 10 }}><Alert tone="wait" icon={<IconAlert width={20} height={20} />} title={r.paused.step_id ? `Stuck at step ${r.paused.step_id}: it needs a person to take over` : "Stuck: it needs a person to take over"}>
          <span>{r.paused.reason}</span>
          <span className="small muted">{atLeast(me?.role, "operator") ? <>Open the run, fix what is in the way on its page, then resume the automation.</> : <>An operator can take over from the run page.</>}{r.paused.stops_in_s != null && ` It stops by itself in about ${Math.max(1, Math.ceil(r.paused.stops_in_s / 60))} min.`}</span>
          {atLeast(me?.role, "operator") && <div><Link className="btn sm primary" href={`/run/?id=${r.id}`}>Take over</Link></div>}
        </Alert></div>
      )}
      {r.awaiting_approval && (() => {
        const ask = r.awaiting_approval, gate = canDecideApproval(me?.role, me?.name, ask.tier, ask.requested_by);
        return (
          <div style={{ marginTop: 10 }}><Alert tone="wait" icon={<IconClock width={20} height={20} />} title={`It stopped before ${ask.description ?? "its last step"}: it needs a ${ask.tier}`}>
            <span>Nothing has been committed. {gate.ok ? "You can review the page and approve it, or work on the page yourself." : `You can't decide this one: ${gate.why?.toLowerCase()}`}</span>
            <div><Link className={`btn sm ${gate.ok ? "primary" : ""}`} href={`/run/?id=${r.id}`}>{gate.ok ? "Review and decide" : "See the run"}</Link></div>
          </Alert></div>
        );
      })()}
      {r.status === "running" && r.pace_ms > 0 && !r.paused && !r.awaiting_approval && <div style={{ marginTop: 10 }}><LiveView runId={r.id} active /></div>}
      {r.status === "pending_approval" && pending && (
        <div className="stack small" style={{ marginTop: 10, gap: 8 }}><span>Nothing has happened yet. It needs a <strong>{pending.tier}</strong> to approve.</span>
          <ApprovalActions run={r} tier={pending.tier} onDone={run.reload} compact /></div>
      )}
      {r.status === "needs_review" && !r.resolution && <div className="stack small" style={{ marginTop: 10, gap: 8 }}><span>The outcome is unknown and it was <strong>not retried</strong>. Check the target system.</span><div><ResolveActions run={r} onDone={run.reload} compact /></div></div>}
      {r.status === "hard_failure" && res?.error && <p className="small" style={{ marginTop: 8, color: "var(--bad)" }}>{res.error.message}</p>}
      {r.status === "business_outcome" && <p className="small" style={{ marginTop: 8 }}>The system answered “{label(res?.business_outcome ?? "")}”: a normal result, not an error.</p>}
      {outputs.length > 0 && <dl className="kv small" style={{ marginTop: 10 }}>{outputs.map(([k, v]) => <div key={k} style={{ display: "contents" }}><dt>{label(k)}</dt><dd>{formatValue(v)}</dd></div>)}</dl>}
    </div>
  );
}

export default function Chat() {
  const { me } = useAuth();
  const toast = useToast();
  const hist = useApi<{ messages: Msg[] }>("/v1/chat");
  const caps = useApi<{ capabilities: Capability[] }>("/v1/capabilities");
  const [extra, setExtra] = useState<Msg[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const features = useApi<Features>("/v1/features");
  const [watch, setWatch] = useWatchPref();
  const bottom = useRef<HTMLDivElement>(null);
  const draftKey = `cua.chatDraft.${me?.name ?? ""}`;
  useEffect(() => { try { setDraft(localStorage.getItem(draftKey) ?? ""); } catch { /* ignore */ } }, [draftKey]);
  useEffect(() => { try { if (draft) localStorage.setItem(draftKey, draft); else localStorage.removeItem(draftKey); } catch { /* ignore */ } }, [draft, draftKey]);
  const messages = [...(hist.data?.messages ?? []), ...extra.filter((e) => !(hist.data?.messages ?? []).some((m) => m.id === e.id))];
  useEffect(() => { bottom.current?.scrollIntoView({ block: "end" }); }, [messages.length, sending]);

  const send = useCallback(async (text: string) => {
    const t = text.trim(); if (!t || sending) return;
    setSending(true);
    try {
      const out = await api<{ messages: Msg[] }>("/v1/chat", { method: "POST", json: { message: t, ...watchBody(watch, features.data) } });
      setExtra((e) => [...e, ...out.messages]); setDraft("");
    } catch (e) { toast(e instanceof ApiError ? e.message : "Could not send", true); }
    finally { setSending(false); }
  }, [sending, toast, watch, features.data]);
  const onSubmit = (e: FormEvent) => { e.preventDefault(); void send(draft); };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send(draft); } };
  async function clear() {
    try { await api("/v1/chat", { method: "DELETE" }); setExtra([]); await hist.reload(); toast("Chat cleared."); } catch (e) { toast(e instanceof ApiError ? e.message : "Could not clear", true); }
    setConfirmClear(false);
  }
  const samples = (caps.data?.capabilities ?? []).some((c) => c.capability_id === "clinic.patient_lookup") ? ["Look up patient LK-100001", "Cancel appointment A-20003 because of weather", "Change the phone number for patient LK-100002 to 206-555-0188"] : [];
  return (
    <div className="page" style={{ maxWidth: 820 }}>
      <PageHead title="Chat" sub={<>A shortcut for one-off requests. It only starts recorded tasks, asks when something is missing, and never approves anything. For tasks with many fields the <Link href="/">task</Link> forms are clearer.</>}>
        <button className="btn sm" onClick={() => setConfirmClear(true)} disabled={messages.length === 0}>Clear chat</button>
      </PageHead>
      <section className="card" style={{ display: "grid", gridTemplateRows: "minmax(320px, 60vh) auto" }} aria-label="Conversation">
        <div style={{ overflowY: "auto", padding: 18, display: "grid", gap: 14, alignContent: "start" }} role="log" aria-live="polite" aria-relevant="additions">
          {hist.error && !hist.data ? <ErrorState error={hist.error} retry={hist.reload} /> : !hist.data ? <Skeleton lines={3} />
            : messages.length === 0 ? <Empty icon={<IconChat />} title="Ask for something">Describe a task in your own words. If a detail is missing I'll ask rather than guess.
              {samples.length > 0 && <span className="chips" style={{ marginTop: 10, justifyContent: "center" }}>{samples.map((s) => <button key={s} className="chip" onClick={() => setDraft(s)}>{s}</button>)}</span>}</Empty>
            : messages.map((m) => (
              <div key={m.id} style={{ display: "grid", justifyItems: m.role === "user" ? "end" : "start" }}>
                <div style={{ maxWidth: "88%", display: "grid", justifyItems: m.role === "user" ? "end" : "start", gap: 2 }}>
                  <span className="small muted">{m.role === "user" ? me?.name : "Assistant"}</span>
                  <div style={{ background: m.role === "user" ? "var(--accent)" : "var(--surface-2)", color: m.role === "user" ? "var(--accent-ink)" : "var(--text)", padding: "9px 13px", borderRadius: 12, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{m.text}</div>
                  {m.run_id && <RunCard runId={m.run_id} />}
                </div>
              </div>))}
          {sending && <div className="small muted" aria-live="polite">Thinking…</div>}
          <div ref={bottom} />
        </div>
        <form onSubmit={onSubmit} style={{ borderTop: "1px solid var(--border)", padding: 12, display: "grid", gap: 10 }}>
          <WatchControl features={features.data} compact pref={watch} setPref={setWatch} />
          <div style={{ display: "flex", gap: 10, alignItems: "flex-end" }}>
            <label className="sr-only" htmlFor="msg">Message</label>
            <textarea id="msg" rows={2} value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={onKey} placeholder="e.g. look up patient LK-100001   (Enter to send, Shift+Enter for a new line)" style={{ resize: "vertical" }} />
            <button className="btn primary" disabled={!draft.trim() || sending}>Send</button>
          </div>
        </form>
      </section>
      <p className="small muted">Your draft is kept if you switch pages. The conversation is saved for your key and survives a restart.</p>
      <Dialog open={confirmClear} onClose={() => setConfirmClear(false)} title="Clear this conversation?" footer={<><button className="btn" onClick={() => setConfirmClear(false)}>Keep it</button><button className="btn danger" onClick={clear}>Clear chat</button></>}>
        <p>This removes the messages from this view. The runs themselves, and their records, are not affected.</p>
      </Dialog>
    </div>
  );
}
