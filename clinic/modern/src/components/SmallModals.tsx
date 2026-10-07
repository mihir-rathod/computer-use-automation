import { useEffect, useState } from "react";
import { api, ApiError, fmtDateTime, type Appointment, type Patient } from "../api";
import { useApp } from "../context";
import { Modal, Problems } from "./Modal";

const messageOf = (e: unknown): string[] =>
  e instanceof ApiError ? (e.problems.length ? e.problems : [e.message]) : ["Something went wrong. Please try again."];

export function ContactModal({ patient, onClose, onSaved }: { patient: Patient; onClose: () => void; onSaved: () => void }) {
  const { toast, t } = useApp();
  const [phone, setPhone] = useState(patient.phone);
  const [email, setEmail] = useState(patient.email);
  const [address, setAddress] = useState(patient.address);
  const [problems, setProblems] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setProblems([]);
    try {
      const r = await api.updateContact(patient.id, { phone, email, address });
      toast(r.changed.length ? "Contact information updated" : "No changes were needed");
      onSaved();
      onClose();
    } catch (err) { setProblems(messageOf(err)); } finally { setBusy(false); }
  };

  return (
    <Modal title={`${t("Update contact")}: ${patient.last_name}, ${patient.first_name}`} onClose={onClose}>
      <form onSubmit={save} noValidate>
        <Problems items={problems} />
        <div className="field"><label htmlFor="c-phone">Phone</label><input id="c-phone" value={phone} onChange={(e) => setPhone(e.target.value)} /></div>
        <div className="field"><label htmlFor="c-email">Email</label><input id="c-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} /></div>
        <div className="field"><label htmlFor="c-address">Address</label><input id="c-address" value={address} onChange={(e) => setAddress(e.target.value)} /></div>
        <div className="actions">
          <button type="button" className="btn" onClick={onClose}>{t("Cancel")}</button>
          <button type="submit" className="btn btn-primary" disabled={busy}>{t("Save changes")}</button>
        </div>
      </form>
    </Modal>
  );
}

export function RescheduleModal({ appointment, onClose, onSaved }: { appointment: Appointment; onClose: () => void; onSaved: () => void }) {
  const { meta, toast, t } = useApp();
  const [date, setDate] = useState("");
  const [slots, setSlots] = useState<string[] | null>(null);
  const [time, setTime] = useState("");
  const [problems, setProblems] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!date) { setSlots(null); return; }
    let cancelled = false;
    setLoading(true);
    setProblems([]);
    api.slots(appointment.number, date)
      .then((r) => { if (!cancelled) { setSlots(r.slots); setTime(r.slots[0] ?? ""); } })
      .catch((err) => { if (!cancelled) { setSlots(null); setProblems(messageOf(err)); } })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [date, appointment.number]);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await api.reschedule(appointment.number, date, time);
      toast(`Appointment ${appointment.number} moved to ${date} ${time}`);
      onSaved();
      onClose();
    } catch (err) { setProblems(messageOf(err)); }
  };

  return (
    <Modal title={`${t("Reschedule")} ${appointment.number}`} onClose={onClose}>
      <p className="muted">{appointment.provider}, currently {fmtDateTime(appointment.starts_at)}</p>
      <form onSubmit={save} noValidate>
        <Problems items={problems} />
        <div className="field">
          <label htmlFor="r-date">New date</label>
          <input id="r-date" type="date" min={meta?.today} value={date} onChange={(e) => setDate(e.target.value)} />
        </div>
        {loading && <p className="muted" aria-busy="true">Loading available times...</p>}
        {slots && slots.length > 0 && (
          <div className="field">
            <label htmlFor="r-time">New time</label>
            <select id="r-time" value={time} onChange={(e) => setTime(e.target.value)}>
              {slots.map((s) => (<option key={s} value={s}>{s}</option>))}
            </select>
          </div>
        )}
        {slots && slots.length === 0 && <p className="muted">No times are available on that date.</p>}
        <div className="actions">
          <button type="button" className="btn" onClick={onClose}>{t("Cancel")}</button>
          <button type="submit" className="btn btn-primary" disabled={!slots || !slots.length || !time}>{t("Save changes")}</button>
        </div>
      </form>
    </Modal>
  );
}

export function SessionExpiredModal() {
  const { sessionLost, resolveSessionLost, user, t } = useApp();
  const [username, setUsername] = useState(user?.username ?? "");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string[]>([]);
  if (!sessionLost) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    try { resolveSessionLost((await api.login(username, password)).user); setPassword(""); setError([]); }
    catch (err) { setError(messageOf(err)); }
  };

  return (
    <Modal title="Your session has expired" onClose={() => undefined}>
      <p>For your security you were signed out after a period of inactivity. Sign in again to continue where you left off.</p>
      <form onSubmit={submit}>
        <Problems items={error} />
        <div className="field"><label htmlFor="s-user">Username</label><input id="s-user" value={username} onChange={(e) => setUsername(e.target.value)} /></div>
        <div className="field"><label htmlFor="s-pass">Password</label><input id="s-pass" type="password" value={password} onChange={(e) => setPassword(e.target.value)} /></div>
        <div className="actions"><button type="submit" className="btn btn-primary">{t("Sign In")}</button></div>
      </form>
    </Modal>
  );
}
