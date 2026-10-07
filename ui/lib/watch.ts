"use client";
import { useCallback, useEffect, useState } from "react";
import type { Features } from "./types";

export type WatchMode = "off" | "slow" | "step";
export interface WatchPref { mode: WatchMode; window: boolean }
const KEY = "cua.watch";
const DEFAULT: WatchPref = { mode: "off", window: false };

/** The person's watch preference, remembered in this browser so it does not reset on every form. */
export function useWatchPref(): [WatchPref, (p: WatchPref) => void] {
  const [pref, setPref] = useState<WatchPref>(DEFAULT);
  useEffect(() => { try { const raw = localStorage.getItem(KEY); if (raw) setPref({ ...DEFAULT, ...JSON.parse(raw) }); } catch { /* ignore */ } }, []);
  const save = useCallback((p: WatchPref) => { setPref(p); try { localStorage.setItem(KEY, JSON.stringify(p)); } catch { /* ignore */ } }, []);
  return [pref, save];
}

export function watchBody(pref: WatchPref, features: Features | null | undefined): { pace_ms: number; show_window: boolean } {
  const presets = features?.watch_presets ?? { slow: 700, step_by_step: 1800 };
  const pace = pref.mode === "slow" ? presets.slow : pref.mode === "step" ? presets.step_by_step : 0;
  return { pace_ms: pace, show_window: !!(pref.window && features?.show_window) };
}
