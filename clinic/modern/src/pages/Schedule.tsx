import { useEffect, useState } from "react";
import { api, ApiError, type ScheduleRow } from "../api";
import { useApp } from "../context";
import { Problems, Skeleton } from "../components/Modal";

export function Schedule() {
  const { meta, t, toast, epoch } = useApp();
  const [date, setDate] = useState(meta?.today ?? "");
  const [provider, setProvider] = useState("");
  const [rows, setRows] = useState<ScheduleRow[] | null>(null);
  const [problems, setProblems] = useState<string[]>([]);

  useEffect(() => { if (!date && meta) setDate(meta.today); }, [meta, date]);

  useEffect(() => {
    if (!date) return;
    let cancelled = false;
    setRows(null);
    api.schedule(date, provider)
      .then((r) => { if (!cancelled) { setRows(r.items); setProblems([]); } })
      .catch((e) => { if (!cancelled) { setRows([]); setProblems(e instanceof ApiError ? (e.problems.length ? e.problems : [e.message]) : ["Could not load the schedule."]); } });
    return () => { cancelled = true; };
  }, [date, provider, epoch]);

  const download = async () => {
    try {
      const blob = await api.scheduleCsv(date, provider);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `schedule-${date}.csv`;
      a.click();
      URL.revokeObjectURL(url);
      toast(`Downloaded schedule-${date}.csv`);
    } catch { toast("The download failed.", "error"); }
  };

  return (
    <section>
      <h1>{t("Today's schedule")}</h1>
      <div className="card search">
        <div className="field"><label htmlFor="s-date">Date</label><input id="s-date" type="date" value={date} onChange={(e) => setDate(e.target.value)} /></div>
        <div className="field"><label htmlFor="s-provider">Provider</label>
          <select id="s-provider" value={provider} onChange={(e) => setProvider(e.target.value)}>
            <option value="">All providers</option>
            {meta?.providers.map((p) => (<option key={p} value={p}>{p}</option>))}
          </select></div>
        <div className="field field-end"><button className="btn" onClick={download} disabled={!rows?.length}>Download CSV</button></div>
      </div>
      <Problems items={problems} />
      {rows === null ? <Skeleton rows={5} /> : rows.length === 0 ? <div className="empty">No appointments scheduled.</div> : (
        <table className="table">
          <caption>{rows.length} appointment{rows.length === 1 ? "" : "s"}</caption>
          <thead><tr><th scope="col">Time</th><th scope="col">Appointment</th><th scope="col">Provider</th><th scope="col">Patient</th><th scope="col">Reason</th><th scope="col">Status</th></tr></thead>
          <tbody>{rows.map((r) => (
            <tr key={r.number}><td>{r.starts_at.slice(11, 16)}</td><td>{r.number}</td><td>{r.provider}</td>
              <td>{r.last_name}, {r.first_name} ({r.mrn})</td><td>{r.reason}</td><td><span className={`pill pill-${r.status}`}>{r.status}</span></td></tr>
          ))}</tbody>
        </table>
      )}
    </section>
  );
}
