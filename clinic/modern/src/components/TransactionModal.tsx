import { useState } from "react";
import { api, ApiError, toCents, type Receipt, type Review } from "../api";
import { useApp } from "../context";
import { Modal, Problems } from "./Modal";

export type Kind = "cancel" | "claim" | "refund" | "writeoff";

interface Props {
  kind: Kind;
  number: string;
  defaultAmount?: string;
  onClose: () => void;
  onDone: () => void;
}

const TITLES: Record<Kind, string> = {
  cancel: "Cancel appointment", claim: "Submit insurance claim", refund: "Issue refund", writeoff: "Write off balance",
};

export function TransactionModal({ kind, number, defaultAmount = "", onClose, onDone }: Props) {
  const { meta, t, toast } = useApp();
  const [step, setStep] = useState<"form" | "review" | "receipt">("form");
  const [reason, setReason] = useState("");
  const [notes, setNotes] = useState("");
  const [amount, setAmount] = useState(defaultAmount);
  const [problems, setProblems] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<{ token: string; review: Review } | null>(null);
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [duplicate, setDuplicate] = useState(false);

  const reasons = kind === "cancel" ? meta?.cancel_reasons : kind === "refund" ? meta?.refund_reasons : kind === "writeoff" ? meta?.writeoff_reasons : undefined;
  const showAmount = kind !== "cancel";

  const fail = (e: unknown) => {
    if (e instanceof ApiError) setProblems(e.problems.length ? e.problems : [e.message]);
    else setProblems(["Something went wrong. Please try again."]);
  };

  const submitForm = async (e: React.FormEvent) => {
    e.preventDefault();
    setProblems([]);
    const cents = showAmount ? toCents(amount) : 0;
    if (showAmount && Number.isNaN(cents)) { setProblems(["Amount must be a number."]); return; }
    const body = kind === "cancel" ? { appointment: number, reason, notes }
      : kind === "claim" ? { invoice: number, amount_cents: cents }
      : { invoice: number, amount_cents: cents, reason, notes };
    setBusy(true);
    try { setDraft(await api.review(kind, body)); setStep("review"); } catch (err) { fail(err); } finally { setBusy(false); }
  };

  const confirm = async () => {
    if (!draft) return;
    setProblems([]);
    setBusy(true);
    try {
      setReceipt(await api.confirm(draft.token));
      setStep("receipt");
    } catch (err) {
      if (err instanceof ApiError && err.code === "already_processed" && err.result) {
        setReceipt(err.result); setDuplicate(true); setStep("receipt");
      } else fail(err);
    } finally { setBusy(false); }
  };

  const back = async () => {
    if (draft) await api.discard(draft.token).catch(() => undefined);
    setDraft(null); setProblems([]); setStep("form");
  };

  const close = () => {
    if (step === "review" && draft) void api.discard(draft.token).catch(() => undefined);
    if (step === "receipt") onDone();
    onClose();
  };

  const done = () => { toast(receipt?.message ?? "Done"); onDone(); onClose(); };

  return (
    <Modal title={`${t(TITLES[kind])} ${number}`} onClose={close}>
      {step === "form" && (
        <form onSubmit={submitForm} noValidate>
          <Problems items={problems} />
          {showAmount && (
            <div className="field">
              <label htmlFor="tx-amount">Amount ($)</label>
              <input id="tx-amount" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} autoComplete="off" />
            </div>
          )}
          {reasons && (
            <>
              <div className="field">
                <label htmlFor="tx-reason">Reason</label>
                <select id="tx-reason" value={reason} onChange={(e) => setReason(e.target.value)}>
                  <option value="">Select a reason</option>
                  {Object.entries(reasons).map(([code, label]) => (<option key={code} value={code}>{label}</option>))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="tx-notes">Notes</label>
                <textarea id="tx-notes" rows={3} value={notes} onChange={(e) => setNotes(e.target.value)} />
              </div>
            </>
          )}
          <div className="actions">
            <button type="button" className="btn" onClick={close}>{t("Cancel")}</button>
            <button type="submit" className="btn btn-primary" disabled={busy}>{busy ? "Checking..." : t("Continue")}</button>
          </div>
        </form>
      )}

      {step === "review" && draft && (
        <div>
          <h3>{draft.review.title}</h3>
          <dl className="lines">
            {draft.review.lines.map((l) => (<div key={l.label}><dt>{l.label}</dt><dd>{l.value}</dd></div>))}
          </dl>
          {draft.review.warnings.map((w) => (<div key={w} className="alert alert-warn" role="alert">{w}</div>))}
          {draft.review.irreversible && <p className="muted">This cannot be undone once confirmed.</p>}
          <Problems items={problems} />
          <div className="actions">
            <button type="button" className="btn" onClick={back} disabled={busy}>{t("Back")}</button>
            <button type="button" className="btn btn-primary" onClick={confirm} disabled={busy}>{busy ? "Working..." : t("Confirm")}</button>
          </div>
        </div>
      )}

      {step === "receipt" && receipt && (
        <div>
          <div className={`alert ${duplicate ? "alert-warn" : "alert-ok"}`} role="status">
            {duplicate ? "This transaction was already processed." : receipt.message}
          </div>
          <dl className="lines">
            <div><dt>Receipt number</dt><dd><strong>{receipt.receipt_number}</strong></dd></div>
            {receipt.lines.map((l) => (<div key={l.label}><dt>{l.label}</dt><dd>{l.value}</dd></div>))}
          </dl>
          <div className="actions"><button type="button" className="btn btn-primary" onClick={done}>Done</button></div>
        </div>
      )}
    </Modal>
  );
}
