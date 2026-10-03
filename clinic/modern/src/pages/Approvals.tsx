import { useCallback, useEffect, useState } from "react";
import { api, ApiError, money, type Approval } from "../api";
import { useApp } from "../context";
import { Skeleton } from "../components/Modal";

export function Approvals() {
  const { t, toast, epoch } = useApp();
  const [items, setItems] = useState<Approval[] | null>(null);
  const [denied, setDenied] = useState(false);
  const [notes, setNotes] = useState<Record<string, string>>({});

  const load = useCallback(() => {
    api.approvals()
      .then((r) => { setItems(r.items); setDenied(false); })
      .catch((e) => { if (e instanceof ApiError && e.status === 403) setDenied(true); else toast("Could not load approvals.", "error"); });
  }, [toast]);

  useEffect(() => { load(); }, [load, epoch]);

  const decide = async (number: string, decision: "approve" | "deny") => {
    try {
      await api.decide(number, decision, notes[number] ?? "");
      toast(`Approval ${number} ${decision === "approve" ? "approved" : "denied"}`);
      load();
    } catch (e) { toast(e instanceof ApiError ? e.message : "Decision failed.", "error"); }
  };

  if (denied) return <div className="alert alert-error" role="alert">Supervisor access is required to view approvals.</div>;
  if (!items) return <Skeleton rows={4} />;

  return (
    <section>
      <h1>{t("Approvals")}</h1>
      {items.length === 0 ? <div className="empty">No approvals are waiting.</div> : (
        <table className="table">
          <thead><tr><th scope="col">Approval</th><th scope="col">Patient</th><th scope="col">Invoice</th><th scope="col" className="num">Amount</th><th scope="col">Requested by</th><th scope="col">Note</th><th scope="col"><span className="sr-only">Decision</span></th></tr></thead>
          <tbody>
            {items.map((a) => (
              <tr key={a.number}>
                <td>{a.number}</td><td>{a.last_name}, {a.first_name} ({a.mrn})</td><td>{a.invoice_number}</td>
                <td className="num">{money(a.amount_cents)}</td><td>{a.requested_by}</td>
                <td><input aria-label={`Decision note for ${a.number}`} value={notes[a.number] ?? ""} onChange={(e) => setNotes({ ...notes, [a.number]: e.target.value })} /></td>
                <td className="row-actions">
                  <button className="btn btn-small btn-primary" aria-label={`Approve ${a.number}`} onClick={() => decide(a.number, "approve")}>{t("Approve")}</button>
                  <button className="btn btn-small" aria-label={`Deny ${a.number}`} onClick={() => decide(a.number, "deny")}>{t("Deny")}</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
