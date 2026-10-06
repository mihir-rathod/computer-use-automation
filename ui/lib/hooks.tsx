"use client";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ApiError, api, clearKey, getKey, setKey } from "./api";
import type { Me } from "./types";

/* ---- auth ---- */
interface AuthState { me: Me | null; status: "loading" | "anon" | "ready"; signIn: (key: string) => Promise<void>; signOut: () => void }
const AuthCtx = createContext<AuthState | null>(null);
export const useAuth = () => { const c = useContext(AuthCtx); if (!c) throw new Error("no AuthProvider"); return c; };

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [status, setStatus] = useState<AuthState["status"]>("loading");
  const load = useCallback(async () => {
    if (!getKey()) { setStatus("anon"); return; }
    try { setMe(await api<Me>("/v1/me")); setStatus("ready"); }
    catch (e) { if (e instanceof ApiError && e.status === 401) { clearKey(); setMe(null); setStatus("anon"); } else { setStatus("anon"); } }
  }, []);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { const f = () => { clearKey(); setMe(null); setStatus("anon"); }; window.addEventListener("cua:unauthorized", f); return () => window.removeEventListener("cua:unauthorized", f); }, []);
  const signIn = useCallback(async (key: string) => {
    const who = await api<Me>("/v1/me", { key: key.trim() });
    setKey(key.trim()); setMe(who); setStatus("ready");
  }, []);
  const signOut = useCallback(() => { clearKey(); setMe(null); setStatus("anon"); }, []);
  const value = useMemo(() => ({ me, status, signIn, signOut }), [me, status, signIn, signOut]);
  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>;
}

/* ---- toasts ---- */
interface Toast { id: number; text: string; bad?: boolean }
const ToastCtx = createContext<(text: string, bad?: boolean) => void>(() => {});
export const useToast = () => useContext(ToastCtx);
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((text: string, bad?: boolean) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, text, bad }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), bad ? 7000 : 4000);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">{toasts.map((t) => <div key={t.id} className={`toast ${t.bad ? "bad" : ""}`}>{t.text}</div>)}</div>
    </ToastCtx.Provider>
  );
}

/* ---- theme ---- */
export type Theme = "system" | "light" | "dark";
export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>("system");
  useEffect(() => { try { setTheme((localStorage.getItem("cua.theme") as Theme) || "system"); } catch { /* ignore */ } }, []);
  const cycle = useCallback(() => {
    setTheme((t) => {
      const next: Theme = t === "system" ? "light" : t === "light" ? "dark" : "system";
      try { localStorage.setItem("cua.theme", next); } catch { /* ignore */ }
      if (next === "system") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", next);
      return next;
    });
  }, []);
  return [theme, cycle];
}

/* ---- data ---- */
export interface Resource<T> { data: T | null; error: ApiError | null; loading: boolean; reload: () => Promise<void> }

/** Fetches a path, optionally polling. A failed poll keeps the last good data and reports the error alongside it. */
export function useApi<T>(path: string | null, opts: { every?: number } = {}): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(path !== null);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const reload = useCallback(async () => {
    if (path === null) return;
    try { const d = await api<T>(path); if (alive.current) { setData(d); setError(null); } }
    catch (e) { if (alive.current) setError(e instanceof ApiError ? e : new ApiError(0, String(e))); }
    finally { if (alive.current) setLoading(false); }
  }, [path]);
  useEffect(() => { setData(null); setError(null); setLoading(path !== null); void reload(); }, [path, reload]);
  useEffect(() => {
    if (!opts.every || path === null) return;
    // A background tab polls a fifth as often rather than not at all: a tab can report itself hidden while someone is still watching it.
    let tick = 0;
    const t = setInterval(() => { tick += 1; if (document.visibilityState === "visible" || tick % 5 === 0) void reload(); }, opts.every);
    return () => clearInterval(t);
  }, [opts.every, path, reload]);
  return { data, error, loading, reload };
}

export function useNow(every = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), every); return () => clearInterval(t); }, [every]);
  return now;
}
