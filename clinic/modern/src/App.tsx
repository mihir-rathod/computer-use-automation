import { BrowserRouter, Navigate, NavLink, Outlet, Route, Routes, useNavigate } from "react-router-dom";
import { api } from "./api";
import { AppProvider, useApp } from "./context";
import { SessionExpiredModal } from "./components/SmallModals";
import { Approvals } from "./pages/Approvals";
import { Login } from "./pages/Login";
import { PatientDetail } from "./pages/PatientDetail";
import { Patients } from "./pages/Patients";
import { Schedule } from "./pages/Schedule";

function Shell() {
  const { user, setUser, t, ready, meta } = useApp();
  const navigate = useNavigate();

  if (!ready) return <p className="muted center" aria-busy="true">Loading...</p>;
  if (!user) return <Navigate to="/login" replace />;

  const signOut = async () => {
    await api.logout().catch(() => undefined);
    setUser(null);
    navigate("/login", { replace: true });
  };

  return (
    <>
      <header className="topbar">
        <strong className="brand">Larkspur Clinic Ops</strong>
        <nav aria-label="Main">
          <NavLink to="/patients">{t("Patient lookup")}</NavLink>
          <NavLink to="/schedule">{t("Today's schedule")}</NavLink>
          {user.role === "supervisor" && <NavLink to="/approvals">{t("Approvals")}</NavLink>}
        </nav>
        <div className="who">
          <span className="muted">{meta?.today}</span>
          <span>{user.display_name} <span className="pill">{user.role}</span></span>
          <button className="btn btn-small" onClick={signOut}>{t("Sign out")}</button>
        </div>
      </header>
      <main className="page"><Outlet /></main>
      <SessionExpiredModal />
    </>
  );
}

export default function App() {
  return (
    <AppProvider>
      <BrowserRouter basename="/app">
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route element={<Shell />}>
            <Route path="/patients" element={<Patients />} />
            <Route path="/patients/:id" element={<PatientDetail />} />
            <Route path="/schedule" element={<Schedule />} />
            <Route path="/approvals" element={<Approvals />} />
          </Route>
          <Route path="*" element={<Navigate to="/patients" replace />} />
        </Routes>
      </BrowserRouter>
    </AppProvider>
  );
}
