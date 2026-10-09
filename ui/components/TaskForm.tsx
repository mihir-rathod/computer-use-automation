"use client";
import { useRouter } from "next/navigation";
import { useMemo, useState, type FormEvent } from "react";
import { ApiError, api } from "@/lib/api";
import { useApi, useToast } from "@/lib/hooks";
import { useWatchPref, watchBody } from "@/lib/watch";
import { WatchControl } from "./WatchControl";
import { buildParams, humanize, kindOf, newKey, placeholderFor, validate, type Values } from "@/lib/schema";
import type { Capability, Features, RunView, Target } from "@/lib/types";
import { Field } from "./ui";

type SubmitResponse = Partial<RunView>;

export function TaskForm({ cap, targets }: { cap: Capability; targets: Target[] }) {
  const router = useRouter();
  const toast = useToast();
  const schema = cap.input_schema;
  const mine = useMemo(() => targets.filter((t) => t.app === cap.app), [targets, cap.app]);
  const [values, setValues] = useState<Values>({});
  const [target, setTarget] = useState(mine[0]?.name ?? "");
  const [dryRun, setDryRun] = useState(false);
  const [key, setKey] = useState(newKey);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const features = useApi<Features>("/v1/features");
  const [watch, setWatch] = useWatchPref();
  const errors = useMemo(() => validate(schema, values), [schema, values]);
  const commits = cap.risk.has_irreversible_step;
  const atStep = commits && cap.risk.approve_at === "step"; // it starts now and stops at the irreversible step, instead of waiting to be approved first
  const names = Object.keys(schema.properties);
  const grouped = new Set(schema.at_least_one_of ?? []);

  async function submit(e: FormEvent) {
    e.preventDefault(); setTouched(true); setServerError(null);
    if (Object.keys(errors).length) { document.querySelector<HTMLElement>("[aria-invalid='true']")?.focus(); return; }
    setBusy(true);
    try {
      const run = await api<SubmitResponse>("/v1/runs", { method: "POST", headers: { "Idempotency-Key": key }, json: { capability_id: cap.capability_id, params: buildParams(schema, values), target, dry_run: dryRun, ...watchBody(watch, features.data) } });
      if (run.id) { router.push(`/run/?id=${run.id}`); return; }
      setServerError(run.result?.error?.message ?? "The request was refused.");
    } catch (err) {
      setServerError(err instanceof ApiError ? err.message : "Could not submit");
      toast(err instanceof ApiError ? err.message : "Could not submit", true);
    } finally { setBusy(false); }
  }

  const set = (name: string, v: string | boolean) => { setValues((s) => ({ ...s, [name]: v })); setServerError(null); };
  const shownError = (n: string) => (touched && errors[n]) || null;

  return (
    <form onSubmit={submit} className="card" noValidate>
      <div className="card-head"><h2>Run this task</h2></div>
      <div className="card-body stack" style={{ gap: 16 }}>
        {schema.at_least_one_of && schema.at_least_one_of.length > 0 && (
          <p className={`small ${touched && errors._group ? "" : "muted"}`} style={touched && errors._group ? { color: "var(--bad)" } : undefined} role={touched && errors._group ? "alert" : undefined}>
            Only fill in what you want to change: the other fields stay exactly as they are. At least one of {schema.at_least_one_of.map((n) => humanize(n).toLowerCase()).join(", ")} is needed.
            {touched && errors._group && <strong style={{ display: "block", marginTop: 4 }}>{errors._group}</strong>}
          </p>
        )}
        {names.map((name) => {
          const prop = schema.properties[name];
          const kind = kindOf(prop);
          const required = schema.required.includes(name);
          const id = `f-${name}`;
          const err = shownError(name);
          const common = { id, "aria-invalid": !!err, "aria-describedby": err ? `${id}-err` : prop.description ? `${id}-hint` : undefined } as const;
          return (
            <Field key={name} id={id} label={humanize(name)} optional={!required} error={err} hint={prop.description}>
              {kind === "enum" ? (
                <select {...common} value={String(values[name] ?? "")} onChange={(e) => set(name, e.target.value)}>
                  <option value="">{required ? "Choose…" : "Leave as it is"}</option>
                  {prop.enum!.map((o) => <option key={o} value={o}>{humanize(o)}</option>)}
                </select>
              ) : kind === "boolean" ? (
                <label className="row" style={{ gap: 8 }}><input type="checkbox" id={id} checked={!!values[name]} onChange={(e) => set(name, e.target.checked)} /> Yes</label>
              ) : (
                <input {...common} type="text" inputMode={kind === "number" ? "decimal" : undefined} autoComplete="off" spellCheck={false}
                  placeholder={placeholderFor(prop) ?? (grouped.has(name) ? "Leave empty to keep the current value" : undefined)}
                  value={String(values[name] ?? "")} onChange={(e) => set(name, e.target.value)} />
              )}
            </Field>
          );
        })}

        {mine.length > 0 && (
          <Field id="f-target" label="Sign in to the system as" hint={mine.find((t) => t.name === target)?.sandbox ? "A sandbox: demo data that can be reset." : undefined}>
            <select id="f-target" value={target} onChange={(e) => setTarget(e.target.value)}>
              {mine.map((t) => <option key={t.name} value={t.name}>{t.signs_in_as} ({t.name})</option>)}
            </select>
          </Field>
        )}

        {commits && (
          <label className="row" style={{ gap: 8, alignItems: "flex-start" }}>
            <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} style={{ marginTop: 4 }} />
            <span><strong>Rehearse only</strong><span className="muted small" style={{ display: "block" }}>Runs every step up to, but not including, the irreversible one. Nothing is changed and no approval is needed.</span></span>
          </label>
        )}

        <WatchControl features={features.data} pref={watch} setPref={setWatch} />
        {commits && !dryRun && !atStep && watch.mode !== "off" && <p className="small muted">This one waits for approval, so you'll watch it once someone approves.</p>}

        <details>
          <summary className="small muted" style={{ cursor: "pointer" }}>Advanced: idempotency key</summary>
          <div className="stack" style={{ marginTop: 10 }}>
            <input aria-label="Idempotency key" type="text" value={key} onChange={(e) => setKey(e.target.value)} className="mono" />
            <p className="hint small muted">Submitting twice with the same key returns the first run instead of doing it again, so a double click or a retry cannot post twice. A new key is made for each form.</p>
          </div>
        </details>

        {serverError && <div className="banner bad" role="alert"><div className="grow"><strong>Couldn't start the run</strong><span>{serverError}</span></div></div>}
        <div className="row">
          <button className="btn primary" disabled={busy}>{busy ? "Submitting…" : commits && !dryRun && !atStep ? `Submit for ${cap.risk.approval_required ?? "approval"} approval` : dryRun ? "Rehearse" : "Run"}</button>
          {commits && !dryRun && !atStep && <span className="small muted">Nothing is done until a {cap.risk.approval_required} approves it.</span>}
          {atStep && !dryRun && <span className="small muted">It runs up to the final step, then waits for a {cap.risk.approval_required} to approve it. Nothing is committed until then.</span>}
        </div>
      </div>
    </form>
  );
}
