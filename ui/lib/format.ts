export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
export function age(iso: string | null | undefined, now = Date.now()): string {
  return ago(iso, now).replace(" ago", "");
}
export function when(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" }) : "—";
}
export function duration(start: string | null, end: string | null): string {
  if (!start || !end) return "—";
  const s = (new Date(end).getTime() - new Date(start).getTime()) / 1000;
  return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}
export const pct = (v: number | null) => (v === null ? "—" : `${Math.round(v * 100)}%`);
export const secs = (v: number | null) => (v === null ? "—" : `${v.toFixed(1)}s`);
export const label = (s: string) => s.replace(/_/g, " ");
export function shortId(id: string): string { return id.replace(/^run_/, "").replace(/^rep_/, ""); }
export function formatValue(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  return typeof v === "object" ? JSON.stringify(v) : String(v);
}
/** "^LK-[0-9]{6}$" -> "LK-" + 6 digits, so a form can say what shape an input takes without showing a regex. */
export function patternHint(pattern?: string): string | null {
  if (!pattern) return null;
  let p = pattern.replace(/^\^|\$$/g, "");
  p = p.replace(/\[0-9\]\{(\d+)\}/g, (_, n) => `${n} digits `).replace(/\[0-9\]\+/g, "digits ");
  p = p.replace(/\\\./g, ".").replace(/\(\.[^)]*\)\?/g, "").replace(/\s+/g, " ").trim();
  return /[\\[\](){}|*+?]/.test(p) ? null : p;
}
