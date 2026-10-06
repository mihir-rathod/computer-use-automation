"use client";
import Link from "next/link";
import { useEffect, useId, useRef, type ReactNode } from "react";
import { ApiError } from "@/lib/api";
import { label } from "@/lib/format";
import { IconAlert, IconCheck, IconClock, IconDot, IconSpinner, IconX, IconEye, IconBolt } from "./icons";

type Tone = "ok" | "bad" | "warn" | "info" | "wait" | "idle";
const STATUS: Record<string, { tone: Tone; text: string; icon: ReactNode }> = {
  success: { tone: "ok", text: "Succeeded", icon: <IconCheck /> },
  business_outcome: { tone: "info", text: "Answered", icon: <IconCheck /> },
  hard_failure: { tone: "bad", text: "Failed", icon: <IconX /> },
  needs_review: { tone: "warn", text: "Needs review", icon: <IconAlert /> },
  pending_approval: { tone: "wait", text: "Awaiting approval", icon: <IconClock /> },
  approved: { tone: "wait", text: "Approved", icon: <IconCheck /> },
  rejected: { tone: "idle", text: "Rejected", icon: <IconX /> },
  queued: { tone: "idle", text: "Queued", icon: <IconClock /> },
  running: { tone: "info", text: "Running", icon: <IconSpinner /> },
  dry_run: { tone: "idle", text: "Dry run", icon: <IconEye /> },
  abandoned: { tone: "idle", text: "Abandoned", icon: <IconX /> },
};
export function StatusPill({ status }: { status: string }) {
  const s = STATUS[status] ?? { tone: "idle" as Tone, text: label(status), icon: <IconDot /> };
  return <span className={`pill ${s.tone}`}>{s.icon}{s.text}</span>;
}
export const isActive = (status: string) => status === "queued" || status === "running" || status === "approved";
export const isSettled = (status: string) => ["success", "business_outcome", "hard_failure", "needs_review", "dry_run", "abandoned", "rejected"].includes(status);

export function RiskBadge({ level, commits, tier }: { level: string; commits: boolean; tier: string | null }) {
  if (commits) return <span className="pill bad"><IconBolt />Irreversible{tier ? ` · ${tier} approval` : ""}</span>;
  if (level === "state_changing") return <span className="pill warn"><IconAlert />Changes data</span>;
  return <span className="pill idle"><IconEye />Read-only</span>;
}

export function Empty({ icon, title, children }: { icon?: ReactNode; title: string; children?: ReactNode }) {
  return <div className="empty">{icon}<strong>{title}</strong>{children && <p>{children}</p>}</div>;
}

export function ErrorState({ error, retry }: { error: ApiError | Error; retry?: () => void }) {
  const status = error instanceof ApiError ? error.status : 0;
  const title = status === 403 ? "You don't have access to this" : status === 404 ? "Not found" : status === 0 ? "Can't reach the platform" : "Something went wrong";
  return (
    <div className="banner bad" role="alert">
      <IconAlert width={20} height={20} />
      <div className="grow"><strong>{title}</strong><span>{error.message}</span></div>
      {retry && <button className="btn sm" onClick={retry}>Try again</button>}
    </div>
  );
}

export function Skeleton({ lines = 3, height = 18 }: { lines?: number; height?: number }) {
  return <div className="stack" aria-busy="true" aria-label="Loading">{Array.from({ length: lines }, (_, i) => <div key={i} className="skeleton" style={{ height, width: `${92 - i * 11}%` }} />)}</div>;
}

export function PageHead({ title, sub, crumb, children }: { title: string; sub?: ReactNode; crumb?: ReactNode; children?: ReactNode }) {
  return <div className="page-head"><div>{crumb && <div className="small" style={{ marginBottom: 4 }}>{crumb}</div>}<h1>{title}</h1>{sub && <p>{sub}</p>}</div><div className="row">{children}</div></div>;
}

export function Dialog({ open, onClose, title, children, footer }: { open: boolean; onClose: () => void; title: string; children: ReactNode; footer?: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const d = ref.current; if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog ref={ref} onClose={onClose} onClick={(e) => { if (e.target === ref.current) onClose(); }} aria-labelledby={titleId}>
      <div className="dlg-head"><h2 id={titleId}>{title}</h2></div>
      <div className="dlg-body">{children}</div>
      {footer && <div className="dlg-foot">{footer}</div>}
    </dialog>
  );
}

export function Field({ id, label: text, hint, error, optional, children }: { id: string; label: string; hint?: string | null; error?: string | null; optional?: boolean; children: ReactNode }) {
  return (
    <div className="field">
      <label htmlFor={id}>{text}{optional && <span className="opt">optional</span>}</label>
      {children}
      {error ? <span className="err" id={`${id}-err`} role="alert">{error}</span> : hint ? <span className="hint" id={`${id}-hint`}>{hint}</span> : null}
    </div>
  );
}

export function CapLink({ id, children }: { id: string; children?: ReactNode }) {
  return <Link href={`/task/?id=${encodeURIComponent(id)}`} className="rowlink">{children ?? id}</Link>;
}
