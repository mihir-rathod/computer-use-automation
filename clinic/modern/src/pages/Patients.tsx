import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type Paged, type Patient } from "../api";
import { useApp } from "../context";
import { Problems, Skeleton } from "../components/Modal";

export function Patients() {
  const { t, epoch } = useApp();
  const [mrn, setMrn] = useState("");
  const [last, setLast] = useState("");
  const [dob, setDob] = useState("");
  const [page, setPage] = useState(1);
  const [result, setResult] = useState<Paged<Patient> | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const searched = Boolean(mrn || last || dob);

  useEffect(() => { setPage(1); }, [mrn, last, dob]);

  useEffect(() => {
    if (!searched) { setResult(null); setProblems([]); return; }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      setLoading(true);
      api.searchPatients({ mrn, last_name: last, dob, page })
        .then((r) => { if (!cancelled) { setResult(r); setProblems([]); } })
        .catch((e) => { if (!cancelled) { setResult(null); setProblems(e instanceof ApiError ? (e.problems.length ? e.problems : [e.message]) : ["Search failed."]); } })
        .finally(() => { if (!cancelled) setLoading(false); });
    }, 300);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [mrn, last, dob, page, searched, epoch]);

  return (
    <section>
      <h1>{t("Patient lookup")}</h1>
      <form className="card search" role="search" onSubmit={(e) => e.preventDefault()}>
        <div className="field"><label htmlFor="q-mrn">MRN</label><input id="q-mrn" placeholder="LK-100001" value={mrn} onChange={(e) => setMrn(e.target.value)} /></div>
        <div className="field"><label htmlFor="q-last">Last name</label><input id="q-last" value={last} onChange={(e) => setLast(e.target.value)} /></div>
        <div className="field"><label htmlFor="q-dob">Date of birth</label><input id="q-dob" type="date" value={dob} onChange={(e) => setDob(e.target.value)} /></div>
      </form>
      <Problems items={problems} />
      {!searched && <p className="muted">Search by MRN, last name or date of birth. Results appear as you type.</p>}
      {loading && <Skeleton rows={4} />}
      {!loading && result && result.total === 0 && <div className="empty">No patients matched your search.</div>}
      {!loading && result && result.total > 0 && (
        <>
          <table className="table">
            <caption>{result.total} patient{result.total === 1 ? "" : "s"} found</caption>
            <thead><tr><th scope="col">MRN</th><th scope="col">Name</th><th scope="col">Date of birth</th><th scope="col">Insurer</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>
              {result.items.map((p) => (
                <tr key={p.id}>
                  <td>{p.mrn}</td><td>{p.last_name}, {p.first_name}</td><td>{p.dob}</td><td>{p.insurer ?? "Self-pay"}</td>
                  <td><Link className="btn btn-small" to={`/patients/${p.id}`} aria-label={`Open ${p.first_name} ${p.last_name}, ${p.mrn}`}>{t("Select")}</Link></td>
                </tr>
              ))}
            </tbody>
          </table>
          <nav className="pager" aria-label="Search results pages">
            <button className="btn btn-small" disabled={result.page <= 1} onClick={() => setPage(result.page - 1)}>{t("Prev")}</button>
            <span>Page {result.page} of {result.pages}</span>
            <button className="btn btn-small" disabled={result.page >= result.pages} onClick={() => setPage(result.page + 1)}>{t("Next")}</button>
          </nav>
        </>
      )}
    </section>
  );
}
