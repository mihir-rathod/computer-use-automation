"use client";
import type { ReactNode } from "react";
import { label } from "@/lib/format";
import { IconAlert, IconCheck, IconClock, IconSpinner, IconX } from "./icons";

export const TEACH_STATUS: Record<string, { tone: string; text: string; icon: ReactNode }> = {
  queued: { tone: "idle", text: "Starting", icon: <IconClock /> },
  running: { tone: "info", text: "Learning", icon: <IconSpinner /> },
  awaiting_commit: { tone: "wait", text: "Needs your answer", icon: <IconAlert /> },
  verifying: { tone: "info", text: "Checking it", icon: <IconSpinner /> },
  ready: { tone: "ok", text: "Ready to review", icon: <IconCheck /> },
  needs_attention: { tone: "warn", text: "Needs attention", icon: <IconAlert /> },
  stuck: { tone: "warn", text: "Got stuck", icon: <IconAlert /> },
  failed: { tone: "bad", text: "Failed", icon: <IconX /> },
  cancelled: { tone: "idle", text: "Stopped", icon: <IconX /> },
  discarded: { tone: "idle", text: "Discarded", icon: <IconX /> },
  promoted: { tone: "ok", text: "In use", icon: <IconCheck /> },
};
export function TeachPill({ status }: { status: string }) {
  const s = TEACH_STATUS[status] ?? { tone: "idle", text: label(status), icon: null };
  return <span className={`pill ${s.tone}`}>{s.icon}{s.text}</span>;
}

