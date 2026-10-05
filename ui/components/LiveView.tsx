"use client";
import { useApi } from "@/lib/hooks";
import type { Timeline } from "@/lib/types";
import { Shot } from "./Panels";

/** The latest page screenshot and the step it is on, updating as the run goes. Screenshots are kept for sandbox targets only. */
export function LiveView({ runId, active, big, onOpen }: { runId: string; active: boolean; big?: boolean; onOpen?: (url: string, alt: string) => void }) {
  const tl = useApi<Timeline>(`/v1/runs/${runId}/timeline`, { every: active ? 1000 : undefined });
  const steps = tl.data?.steps ?? [];
  const withShot = [...steps].reverse().find((s) => s.screenshot);
  const shot = withShot?.screenshot ?? tl.data?.sign_on?.screenshot ?? null;
  const current = active ? steps.find((s) => s.status === "not_run") : null;
  const done = steps.filter((s) => s.status === "ok").length;
  return (
    <div className="stack" style={{ gap: 8 }} aria-live="off">
      <div className="small" style={{ minHeight: 20 }}>
        {active ? <><span className="muted">Now: </span><strong>{current ? current.description : "finishing up"}</strong>{steps.length > 0 && <span className="muted"> · step {Math.min(done + 1, steps.length)} of {steps.length}</span>}</> : <span className="muted">The last page the run saw</span>}
      </div>
      {shot ? <Shot live={big} big={!big} path={`/v1/runs/${runId}/screenshots/${shot}`} alt="Latest page" onOpen={onOpen} />
        : <div className="small muted">{tl.data ? "No picture yet: screenshots appear after the first step, and only for sandbox systems." : "Loading…"}</div>}
    </div>
  );
}
