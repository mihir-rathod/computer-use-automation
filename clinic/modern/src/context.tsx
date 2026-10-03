import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, setSessionLostHandler, type Meta, type User } from "./api";

interface Toast { id: number; kind: "success" | "error"; message: string }

interface AppState {
  user: User | null;
  setUser: (u: User | null) => void;
  meta: Meta | null;
  t: (text: string) => string;
  toast: (message: string, kind?: "success" | "error") => void;
  sessionLost: boolean;
  resolveSessionLost: (u: User) => void;
  epoch: number;
  ready: boolean;
}

const Ctx = createContext<AppState | null>(null);
export const useApp = () => {
  const v = useContext(Ctx);
  if (!v) throw new Error("useApp outside provider");
  return v;
};

export function AppProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [text, setText] = useState<Record<string, string>>({});
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [sessionLost, setSessionLost] = useState(false);
  const [epoch, setEpoch] = useState(0);
  const [ready, setReady] = useState(false);
  const nextId = useRef(1);
  const userRef = useRef<User | null>(null);
  userRef.current = user;

  useEffect(() => {
    setSessionLostHandler(() => { if (userRef.current) setSessionLost(true); });
    Promise.allSettled([api.me(), api.meta(), api.uiText()]).then(([me, meta, ui]) => {
      if (me.status === "fulfilled") setUser(me.value.user);
      if (meta.status === "fulfilled") setMeta(meta.value);
      if (ui.status === "fulfilled") setText(ui.value.text);
      setReady(true);
    });
    return () => setSessionLostHandler(null);
  }, []);

  const toast = useCallback((message: string, kind: "success" | "error" = "success") => {
    const id = nextId.current++;
    setToasts((all) => [...all, { id, kind, message }]);
    window.setTimeout(() => setToasts((all) => all.filter((x) => x.id !== id)), 4500);
  }, []);

  const value = useMemo<AppState>(() => ({
    user, setUser, meta, toast, sessionLost, epoch, ready,
    t: (s: string) => text[s] ?? s,
    resolveSessionLost: (u: User) => { setUser(u); setSessionLost(false); setEpoch((n) => n + 1); },
  }), [user, meta, text, toast, sessionLost, epoch, ready]);

  return (
    <Ctx.Provider value={value}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((x) => (<div key={x.id} className={`toast toast-${x.kind}`}>{x.message}</div>))}
      </div>
    </Ctx.Provider>
  );
}
