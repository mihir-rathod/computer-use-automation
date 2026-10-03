import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError, fmtDateTime, money, type Appointment, type PatientDetail as Detail } from "../api";
import { useApp } from "../context";
import { Skeleton } from "../components/Modal";
import { ContactModal, RescheduleModal } from "../components/SmallModals";
import { TransactionModal, type Kind } from "../components/TransactionModal";

type Tab = "appointments" | "billing" | "history";
type Dialog =
  | { type: "contact" }
  | { type: "reschedule"; appointment: Appointment }
  | { type: "tx"; kind: Kind; number: string; amount?: string }
  | null;

export function PatientDetail() {
  const { id } = useParams();
  const { t, epoch } = useApp();
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("appointments");
  const [invoicePage, setInvoicePage] = useState(1);
  const [dialog, setDialog] = useState<Dialog>(null);

  const load = useCallback(() => {
    api.patient(Number(id), invoicePage)
      .then((d) => { setDetail(d); setError(null); })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load this patient."));
  }, [id, invoicePage]);

  useEffect(() => { load(); }, [load, epoch]);

  if (error) return <div className="alert alert-error" role="alert">{error} <Link to="/patients">Back to search</Link></div>;
  if (!detail) return <Skeleton rows={6} />;

  const { patient: p } = detail;
  const tabs: { id: Tab; label: string }[] = [
    { id: "appointments", label: "Appointments" }, { id: "billing", label: "Billing" }, { id: "history", label: "Claims and refunds" },
  ];

  return (
    <section>
      <p><Link to="/patients">&larr; {t("Back")} to search</Link></p>
      <div className="card patient-head">
        <div>
          <h1>{p.last_name}, {p.first_name}</h1>
          <p className="muted">{p.mrn} &middot; Born {p.dob} &middot; {p.insurer ? `${p.insurer} ${p.insurance_member_id ?? ""}` : "Self-pay"}</p>
          <p>{p.phone} &middot; {p.email}<br />{p.address}</p>
        </div>
        <div className="head-side">
          <div className="balance"><span className="muted">Balance due</span><strong>{money(detail.balance_cents)}</strong></div>
          <button className="btn" onClick={() => setDialog({ type: "contact" })}>{t("Update contact")}</button>
        </div>
      </div>

      <div role="tablist" aria-label="Patient sections" className="tabs">
        {tabs.map((x) => (
          <button key={x.id} role="tab" id={`tab-${x.id}`} aria-selected={tab === x.id} aria-controls={`panel-${x.id}`}
                  className={tab === x.id ? "tab tab-active" : "tab"} onClick={() => setTab(x.id)}>{x.label}</button>
        ))}
      </div>

      {tab === "appointments" && (
        <div role="tabpanel" id="panel-appointments" aria-labelledby="tab-appointments">
          {detail.appointments.length === 0 ? <div className="empty">No appointments on file.</div> : (
            <table className="table">
              <thead><tr><th scope="col">Appointment</th><th scope="col">When</th><th scope="col">Provider</th><th scope="col">Reason</th><th scope="col">Status</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
              <tbody>
                {detail.appointments.map((a) => (
                  <tr key={a.id}>
                    <td>{a.number}</td><td>{fmtDateTime(a.starts_at)}</td><td>{a.provider}</td><td>{a.reason}</td>
                    <td><span className={`pill pill-${a.status}`}>{a.status}</span></td>
                    <td className="row-actions">
                      {a.can_modify && (<>
                        <button className="btn btn-small" aria-label={`Cancel appointment ${a.number}`} onClick={() => setDialog({ type: "tx", kind: "cancel", number: a.number })}>{t("Cancel")}</button>
                        <button className="btn btn-small" aria-label={`Reschedule appointment ${a.number}`} onClick={() => setDialog({ type: "reschedule", appointment: a })}>{t("Reschedule")}</button>
                      </>)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {tab === "billing" && (
        <div role="tabpanel" id="panel-billing" aria-labelledby="tab-billing">
          {detail.invoices.items.length === 0 ? <div className="empty">No invoices on file.</div> : (
            <>
              <table className="table">
                <thead><tr><th scope="col">Invoice</th><th scope="col">Description</th><th scope="col" className="num">Amount</th><th scope="col" className="num">Paid</th><th scope="col" className="num">Balance</th><th scope="col">Claim</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
                <tbody>
                  {detail.invoices.items.map((i) => (
                    <tr key={i.id}>
                      <td>{i.number}</td><td>{i.description}</td><td className="num">{money(i.amount_cents)}</td>
                      <td className="num">{money(i.paid_cents)}</td><td className="num">{money(i.balance_cents)}</td>
                      <td>{i.claimed ? <span className="pill pill-ok">submitted</span> : "-"}</td>
                      <td className="row-actions">
                        {!i.claimed && i.balance_cents > 0 && (
                          <button className="btn btn-small" aria-label={`Submit claim for invoice ${i.number}`}
                                  onClick={() => setDialog({ type: "tx", kind: "claim", number: i.number, amount: (i.balance_cents / 100).toFixed(2) })}>{t("Claim")}</button>)}
                        {i.refundable_cents > 0 && (
                          <button className="btn btn-small" aria-label={`Issue refund for invoice ${i.number}`}
                                  onClick={() => setDialog({ type: "tx", kind: "refund", number: i.number })}>{t("Refund")}</button>)}
                        {i.balance_cents > 0 && (
                          <button className="btn btn-small" aria-label={`Write off invoice ${i.number}`}
                                  onClick={() => setDialog({ type: "tx", kind: "writeoff", number: i.number })}>{t("Write off")}</button>)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <nav className="pager" aria-label="Invoice pages">
                <button className="btn btn-small" disabled={detail.invoices.page <= 1} onClick={() => setInvoicePage(detail.invoices.page - 1)}>{t("Prev")}</button>
                <span>Page {detail.invoices.page} of {detail.invoices.pages}</span>
                <button className="btn btn-small" disabled={detail.invoices.page >= detail.invoices.pages} onClick={() => setInvoicePage(detail.invoices.page + 1)}>{t("Next")}</button>
              </nav>
            </>
          )}
        </div>
      )}

      {tab === "history" && (
        <div role="tabpanel" id="panel-history" aria-labelledby="tab-history">
          <h2>Claims</h2>
          {detail.claims.length === 0 ? <div className="empty">No claims submitted.</div> : (
            <table className="table"><thead><tr><th scope="col">Claim</th><th scope="col">Invoice</th><th scope="col">Payer</th><th scope="col" className="num">Amount</th><th scope="col">Status</th></tr></thead>
              <tbody>{detail.claims.map((c) => (<tr key={c.number}><td>{c.number}</td><td>{c.invoice_number}</td><td>{c.payer}</td><td className="num">{money(c.amount_cents)}</td><td>{c.status}</td></tr>))}</tbody></table>
          )}
          <h2>Refunds</h2>
          {detail.refunds.length === 0 ? <div className="empty">No refunds.</div> : (
            <table className="table"><thead><tr><th scope="col">Refund</th><th scope="col">Invoice</th><th scope="col" className="num">Amount</th><th scope="col">Status</th></tr></thead>
              <tbody>{detail.refunds.map((r) => (<tr key={r.number}><td>{r.number}</td><td>{r.invoice_number}</td><td className="num">{money(r.amount_cents)}</td><td><span className={`pill pill-${r.status}`}>{r.status.replace("_", " ")}</span></td></tr>))}</tbody></table>
          )}
        </div>
      )}

      {dialog?.type === "contact" && <ContactModal patient={p} onClose={() => setDialog(null)} onSaved={load} />}
      {dialog?.type === "reschedule" && <RescheduleModal appointment={dialog.appointment} onClose={() => setDialog(null)} onSaved={load} />}
      {dialog?.type === "tx" && <TransactionModal kind={dialog.kind} number={dialog.number} defaultAmount={dialog.amount} onClose={() => setDialog(null)} onDone={load} />}
    </section>
  );
}
