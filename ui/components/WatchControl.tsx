"use client";
import type { Features } from "@/lib/types";
import { useWatchPref, type WatchMode } from "@/lib/watch";

const MODES: { mode: WatchMode; label: string }[] = [{ mode: "off", label: "Off" }, { mode: "slow", label: "Slow" }, { mode: "step", label: "Step by step" }];

/** Watch mode: slows the run down and outlines each element it touches, with a live view on the run page. It never changes what the run does. */
export function WatchControl({ features, compact, pref, setPref }: { features: Features | null | undefined; compact?: boolean; pref: ReturnType<typeof useWatchPref>[0]; setPref: ReturnType<typeof useWatchPref>[1] }) {
  return (
    <fieldset style={{ border: 0, padding: 0, margin: 0, display: "grid", gap: compact ? 4 : 8 }}>
      <legend className="label" style={{ padding: 0 }}>Watch it run</legend>
      <div className="chips" role="radiogroup" aria-label="Watch it run">
        {MODES.map((m) => <button key={m.mode} type="button" role="radio" aria-checked={pref.mode === m.mode} aria-pressed={pref.mode === m.mode} className="chip" onClick={() => setPref({ ...pref, mode: m.mode })}>{m.label}</button>)}
      </div>
      {!compact && <span className="hint small muted">{pref.mode === "off" ? "Runs at full speed." : "Slows the run down and outlines each element it touches, with a live view on the run page. What it does is unchanged."}</span>}
      {features?.show_window && pref.mode !== "off" && (
        <label className="row small" style={{ gap: 8 }}><input type="checkbox" checked={pref.window} onChange={(e) => setPref({ ...pref, window: e.target.checked })} />Also open the browser window on the machine running the server</label>
      )}
    </fieldset>
  );
}
