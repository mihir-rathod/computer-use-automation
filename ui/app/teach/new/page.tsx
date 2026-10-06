"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { useApi, useAuth, useToast } from "@/lib/hooks";
import { atLeast, type TeachContract, type TeachEffect, type TeachOptions, type TeachSession } from "@/lib/types";
import { Empty, ErrorState, Field, PageHead, Skeleton } from "@/components/ui";
import { IconBolt, IconSpinner, IconX } from "@/components/icons";

interface InputRow { name: string; kind: "text" | "number"; required: boolean; example: string; pattern: string; choices: string; description: string }
interface OutRow { name: string; description: string }
interface OutcomeRow { when_text: string; outcome: string }
type Check = "same" | "second" | "none";

const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").replace(/^[0-9]+/, "").slice(0, 40);
const blankInput = (): InputRow => ({ name: "", kind: "text", required: true, example: "", pattern: "", choices: "", description: "" });
const EFFECTS: { key: TeachEffect; title: string; text: string }[] = [
  { key: "read_only", title: "It only reads", text: "Looks something up and gives it back. The model is not allowed to press anything that changes data." },
  { key: "changes_data", title: "It changes data", text: "Changes something that can be corrected afterwards." },
  { key: "irreversible", title: "It commits something", text: "Cannot be undone from here, such as a refund or a submitted claim. Every run will need a supervisor's approval." },
];

function NewTask() {
  const router = useRouter();
  const toast = useToast();
  const { me } = useAuth();
  const retry = useSearchParams().get("retry");
  const opts = useApi<TeachOptions>("/v1/teach/options");
  const prev = useApi<TeachSession>(retry ? `/v1/teach/${retry}` : null);
  const [stage, setStage] = useState<"describe" | "review">(retry ? "review" : "describe");
  const [request, setRequest] = useState(""); const [drafting, setDrafting] = useState(false); const [question, setQuestion] = useState<string | null>(null);
  const [target, setTarget] = useState("");
  const [name, setName] = useState(""); const [taskName, setTaskName] = useState(""); const [taskTouched, setTaskTouched] = useState(false);
  const [description, setDescription] = useState(""); const [goal, setGoal] = useState(""); const [startPath, setStartPath] = useState("");
  const [inputs, setInputs] = useState<InputRow[]>([blankInput()]);
  const [outputs, setOutputs] = useState<OutRow[]>([{ name: "", description: "" }]);
  const [successText, setSuccessText] = useState(""); const [successStatus, setSuccessStatus] = useState("done");
  const [outcomes, setOutcomes] = useState<OutcomeRow[]>([]);
  const [effect, setEffect] = useState<TeachEffect>("read_only"); const [commitApproval, setCommitApproval] = useState<"auto_sandbox" | "supervisor">("supervisor");
  const [check, setCheck] = useState<Check>("same"); const [verifyInputs, setVerifyInputs] = useState<Record<string, string>>({}); const [expectOutcome, setExpectOutcome] = useState("");
  const [hint, setHint] = useState(""); const [retryText, setRetryText] = useState(""); const [timeoutText, setTimeoutText] = useState("");
  const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  function fill(c: TeachContract) {
    setTarget(c.target); setName(c.name); setTaskName(c.task_name); setTaskTouched(true); setDescription(c.description); setGoal(c.goal); setStartPath(c.start_path ?? "");
    setInputs(c.inputs.length ? c.inputs.map((i) => ({ name: i.name, kind: i.kind, required: i.required, example: i.example, pattern: i.pattern ?? "", choices: (i.choices ?? []).join(", "), description: i.description ?? "" })) : [blankInput()]);
    setOutputs(c.outputs.length ? c.outputs.map((o) => ({ name: o.name, description: o.description ?? "" })) : [{ name: "", description: "" }]);
    setSuccessText(c.success_text ?? ""); setSuccessStatus(c.success_status); setOutcomes(c.outcomes); setEffect(c.effect); setCommitApproval(c.commit_approval);
    setCheck(c.verify?.same_as_example ? "same" : c.verify ? "second" : "none"); setVerifyInputs(c.verify?.inputs ?? {}); setExpectOutcome(c.verify?.expect_outcome ?? "");
    setHint(c.hint ?? ""); setRetryText(c.retry_on_text ?? ""); setTimeoutText(c.timeout_text ?? "");
  }
  // trying again: start from what was submitted last time
  useEffect(() => { const c = prev.data?.contract; if (c && !loaded) { setLoaded(true); fill(c); } }, [prev.data, loaded]);
  useEffect(() => { if (!target && opts.data?.targets[0] && !retry) setTarget(opts.data.targets[0].name); }, [opts.data, target, retry]);
  useEffect(() => { if (!taskTouched) setTaskName(slug(name)); }, [name, taskTouched]);

  const tgt = opts.data?.targets.find((t) => t.name === target);
  const app = tgt?.app_id ?? "app";
  const inputNames = inputs.map((i) => i.name.trim()).filter(Boolean);
  const readOnlyOnly = !!tgt && !tgt.sandbox;
  useEffect(() => { if (readOnlyOnly && effect !== "read_only") setEffect("read_only"); }, [readOnlyOnly, effect]);
  useEffect(() => { if (effect !== "read_only" && check === "same") setCheck("none"); }, [effect, check]);

  const contract = useMemo<TeachContract>(() => ({
    task_name: taskName.trim(), name: name.trim(), description: description.trim() || name.trim(), target, goal: goal.trim(), start_path: startPath.trim() || null,
    inputs: inputs.filter((i) => i.name.trim()).map((i) => ({ name: i.name.trim(), kind: i.kind, required: i.required, example: i.example.trim(), description: i.description.trim() || null, pattern: i.pattern.trim() || null, choices: i.choices.trim() ? i.choices.split(",").map((c) => c.trim()).filter(Boolean) : null })),
    outputs: outputs.filter((o) => o.name.trim()).map((o) => ({ name: o.name.trim(), description: o.description.trim() || null })),
    success_text: successText.trim() || null, success_status: successStatus.trim() || "done", outcomes: outcomes.filter((o) => o.when_text.trim() && o.outcome.trim()).map((o) => ({ when_text: o.when_text.trim(), outcome: o.outcome.trim() })),
    effect, commit_approval: commitApproval, hint: hint.trim() || null, retry_on_text: retryText.trim() || null, timeout_text: timeoutText.trim() || null,
    verify: check === "none" ? null : check === "same" ? { inputs: {}, same_as_example: true } : { inputs: Object.fromEntries(Object.entries(verifyInputs).filter(([k, v]) => inputNames.includes(k) && v.trim()).map(([k, v]) => [k, v.trim()])), expect_outcome: expectOutcome || null },
  }), [taskName, name, description, target, goal, startPath, inputs, outputs, successText, successStatus, outcomes, effect, commitApproval, hint, retryText, timeoutText, check, verifyInputs, expectOutcome, inputNames]);

  if (!atLeast(me?.role, "supervisor")) return <div className="page"><Empty title="Discovery needs a supervisor" /></div>;
  if (opts.error) return <div className="page"><ErrorState error={opts.error} retry={opts.reload} /></div>;
  if (!opts.data || (retry && !prev.data && !prev.error)) return <div className="page"><Skeleton lines={6} /></div>;
  const o = opts.data;
  const blocked = o.busy || !o.model_available;

  async function draftIt(e: React.FormEvent) {
    e.preventDefault(); setError(null); setQuestion(null); setDrafting(true);
    try {
      const r = await api<{ contract?: TeachContract; question?: string }>("/v1/teach/draft", { method: "POST", json: { target, request } });
      if (r.question) setQuestion(r.question);
      else if (r.contract) { fill(r.contract); setStage("review"); }
    } catch (err) { setError(err instanceof ApiError ? err.message : "Could not draft it"); }
    finally { setDrafting(false); }
  }
  async function submit(e: React.FormEvent) {
    e.preventDefault(); setError(null); setBusy(true);
    try {
      const s = await api<TeachSession>("/v1/teach", { method: "POST", json: { contract, retry_of: retry } });
      router.push(`/teach/session/?id=${s.id}`);
    } catch (err) { const m = err instanceof ApiError ? err.message : "Could not start"; setError(m); toast(m, true); window.scrollTo({ top: 0, behavior: "smooth" }); }
    finally { setBusy(false); }
  }
  const upd = <T,>(list: T[], set: (v: T[]) => void, i: number, patch: Partial<T>) => set(list.map((x, j) => (j === i ? { ...x, ...patch } : x)));

  return (
    <div className="page">
      <PageHead title={retry ? "Discover it again" : "Discover a new task"} crumb={<Link href="/teach/">← Discovery</Link>}
        sub="Say what you want done in your own words. The model works out the clicks once on a practice system, and every run after that replays them with no model." />
      {error && <div className="banner bad" role="alert"><div className="grow"><strong>That did not work</strong><span>{error}</span></div></div>}
      {!o.model_available && <div className="banner warn" role="status"><div className="grow"><strong>No model is configured</strong><span>Set GEMINI_API_KEY on the server first.</span></div></div>}
      {o.busy && <div className="banner warn" role="status"><div className="grow"><strong>Another session is running</strong><span>One task is discovered at a time. You can prepare this one, but it cannot start yet.</span></div></div>}

      {stage === "describe" && (
        <form onSubmit={draftIt} className="card" noValidate>
          <div className="card-body stack" style={{ gap: 16 }}>
            <Field id="t-target" label="Which system?" hint={tgt && !tgt.sandbox ? "Not a practice system: only read-only tasks can be discovered here." : "A practice copy: nothing real is touched while it learns."}>
              <select id="t-target" value={target} onChange={(e) => setTarget(e.target.value)}>{o.targets.map((t) => <option key={t.name} value={t.name}>{t.name}{t.sandbox ? " (practice)" : ""}</option>)}</select></Field>
            <Field id="t-request" label="What should it do?" hint="Include a real example value, such as an appointment number, so it can try it. Say what to read back or change.">
              <textarea id="t-request" rows={4} value={request} onChange={(e) => setRequest(e.target.value)} placeholder="Look up appointment A-20002 and tell me the patient and the provider" autoFocus /></Field>
            {question && <div className="banner info" role="status"><div className="grow"><strong>One thing first</strong><span>{question} Add it to your description above.</span></div></div>}
            <div className="row">
              <button className="btn primary" type="submit" disabled={drafting || request.trim().length < 10 || !o.model_available}>{drafting ? <><IconSpinner width={16} height={16} /> Reading the system…</> : <><IconBolt width={16} height={16} /> Draft the task</>}</button>
              <button type="button" className="btn ghost" onClick={() => setStage("review")}>I&apos;ll fill in the details myself</button>
            </div>
          </div>
        </form>)}

      {stage === "review" && (
        <form onSubmit={submit} className="stack" style={{ gap: 16 }} noValidate>
          {!retry && <div className="banner info"><div className="grow"><strong>Check this is what you meant</strong><span>This is the model&apos;s draft. Correct anything that is off, especially whether the task only reads, then start.</span></div></div>}
          {retry && <div className="banner info"><div className="grow"><strong>Starting from the last attempt</strong><span>Add a hint under More details about where it went wrong: the model reads it as extra guidance.</span></div></div>}
          <section className="card"><div className="card-head"><h2>The task</h2></div><div className="card-body stack" style={{ gap: 14 }}>
            <Field id="t-name" label="Name"><input id="t-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={80} /></Field>
            <Field id="t-goal" label="What it will do" hint="Plain steps, the way you would tell a new colleague."><textarea id="t-goal" rows={4} value={goal} onChange={(e) => setGoal(e.target.value)} /></Field>
            <div className="grid-2">
              <div className="stack" style={{ gap: 10 }}>
                <div className="label">It takes</div>
                {inputs.map((i, k) => (
                  <div key={k} className="row" style={{ alignItems: "flex-end" }}>
                    <Field id={`i-n${k}`} label={k === 0 ? "Name" : "Name "}><input id={`i-n${k}`} className="mono" value={i.name} onChange={(e) => upd(inputs, setInputs, k, { name: e.target.value })} /></Field>
                    <Field id={`i-e${k}`} label={k === 0 ? "Example" : "Example "}><input id={`i-e${k}`} value={i.example} onChange={(e) => upd(inputs, setInputs, k, { example: e.target.value })} /></Field>
                    <button type="button" className="btn sm ghost" onClick={() => setInputs(inputs.filter((_, j) => j !== k))} aria-label={`Remove input ${k + 1}`}><IconX width={14} height={14} /></button>
                  </div>))}
                <div><button type="button" className="btn sm" onClick={() => setInputs([...inputs, blankInput()])}>Add an input</button></div>
              </div>
              <div className="stack" style={{ gap: 10 }}>
                <div className="label">It gives back</div>
                {outputs.map((x, k) => (
                  <div key={k} className="row" style={{ alignItems: "flex-end" }}>
                    <Field id={`o-n${k}`} label={k === 0 ? "Name" : "Name "}><input id={`o-n${k}`} className="mono" value={x.name} onChange={(e) => upd(outputs, setOutputs, k, { name: e.target.value })} /></Field>
                    <button type="button" className="btn sm ghost" onClick={() => setOutputs(outputs.filter((_, j) => j !== k))} aria-label={`Remove output ${k + 1}`}><IconX width={14} height={14} /></button>
                  </div>))}
                <div><button type="button" className="btn sm" onClick={() => setOutputs([...outputs, { name: "", description: "" }])}>Add an output</button></div>
              </div>
            </div>
          </div></section>
          <section className="card"><div className="card-head"><h2>Does it change anything?</h2></div><div className="card-body stack" style={{ gap: 12 }}>
            <div className="stack" role="radiogroup" aria-label="What the task does" style={{ gap: 8 }}>
              {EFFECTS.map((x) => (
                <label key={x.key} className="card card-pad row" style={{ alignItems: "flex-start", cursor: readOnlyOnly && x.key !== "read_only" ? "not-allowed" : "pointer", opacity: readOnlyOnly && x.key !== "read_only" ? 0.55 : 1 }}>
                  <input type="radio" name="effect" checked={effect === x.key} disabled={readOnlyOnly && x.key !== "read_only"} onChange={() => setEffect(x.key)} />
                  <span className="stack" style={{ gap: 2 }}><strong>{x.title}</strong><span className="small muted">{x.text}</span></span>
                </label>))}
            </div>
            {effect !== "read_only" && (
              <Field id="t-commit" label="When the model reaches the final step" hint="The step is really taken on the practice system while it learns, so what it records is real.">
                <select id="t-commit" value={commitApproval} onChange={(e) => setCommitApproval(e.target.value as "auto_sandbox" | "supervisor")}>
                  <option value="supervisor">Ask a supervisor first (recommended)</option><option value="auto_sandbox">Go ahead by itself: the practice system resets</option></select></Field>)}
          </div></section>
          <details className="card"><summary className="card-head" style={{ cursor: "pointer" }}><h2>More details (optional)</h2></summary>
            <div className="card-body stack" style={{ gap: 14 }}>
              <div className="grid-2">
                <Field id="t-target2" label="System"><select id="t-target2" value={target} onChange={(e) => setTarget(e.target.value)}>{o.targets.map((t) => <option key={t.name} value={t.name}>{t.name}{t.sandbox ? " (practice)" : ""}</option>)}</select></Field>
                <Field id="t-id" label="Short id" hint={`Saved as ${app}.${taskName || "…"}`}><input id="t-id" className="mono" value={taskName} onChange={(e) => { setTaskTouched(true); setTaskName(e.target.value); }} maxLength={40} /></Field>
              </div>
              <Field id="t-desc" label="What it is for"><input id="t-desc" value={description} onChange={(e) => setDescription(e.target.value)} maxLength={300} /></Field>
              <Field id="t-path" label="Start on this page" optional hint="Empty: the system's main page, and the model finds its own way."><input id="t-path" className="mono" value={startPath} onChange={(e) => setStartPath(e.target.value)} placeholder="/legacy/fn/cancel" /></Field>
              <Field id="s-text" label="Text on the page when it worked" optional hint="Empty: decided from the page the answer is read from, then proved by a replay."><input id="s-text" value={successText} onChange={(e) => setSuccessText(e.target.value)} /></Field>
              {inputs.map((i, k) => (
                <div key={k} className="grid-2">
                  <Field id={`i-p${k}`} label={`${i.name || `Input ${k + 1}`} must look like`} optional hint="A pattern, to catch typos before a run starts."><input id={`i-p${k}`} className="mono" value={i.pattern} onChange={(e) => upd(inputs, setInputs, k, { pattern: e.target.value })} placeholder="^A-[0-9]{5}$" /></Field>
                  <Field id={`i-c${k}`} label={`${i.name || `Input ${k + 1}`}: or one of`} optional hint="Comma-separated choices."><input id={`i-c${k}`} value={i.choices} onChange={(e) => upd(inputs, setInputs, k, { choices: e.target.value })} /></Field>
                </div>))}
              <div className="stack" style={{ gap: 10 }}>
                <div className="label">Normal answers the page can give instead</div>
                {outcomes.map((x, k) => (
                  <div key={k} className="row" style={{ alignItems: "flex-end" }}>
                    <Field id={`oc-t${k}`} label="When the page says"><input id={`oc-t${k}`} value={x.when_text} onChange={(e) => upd(outcomes, setOutcomes, k, { when_text: e.target.value })} placeholder="RECORD NOT FOUND" /></Field>
                    <Field id={`oc-o${k}`} label="It is the answer"><input id={`oc-o${k}`} className="mono" value={x.outcome} onChange={(e) => upd(outcomes, setOutcomes, k, { outcome: e.target.value })} placeholder="not_found" /></Field>
                    <button type="button" className="btn sm ghost" onClick={() => setOutcomes(outcomes.filter((_, j) => j !== k))} aria-label={`Remove answer ${k + 1}`}><IconX width={14} height={14} /></button>
                  </div>))}
                <div><button type="button" className="btn sm" onClick={() => setOutcomes([...outcomes, { when_text: "", outcome: "" }])}>Add a normal answer</button></div>
              </div>
              <Field id="v-mode" label="Check the recording afterwards" hint="The recording is replayed once with no model, to prove it works on its own.">
                <select id="v-mode" value={check} onChange={(e) => setCheck(e.target.value as Check)}>
                  <option value="same" disabled={effect !== "read_only"}>Replay it with the same example (read-only tasks)</option>
                  <option value="second">Replay it with different values</option><option value="none">Do not replay it</option></select></Field>
              {check === "second" && (<>
                {inputNames.length === 0 ? <p className="small muted">Add an input to give it different values.</p> : <div className="grid-2">{inputs.filter((i) => i.name.trim()).map((i) => (
                  <Field key={i.name} id={`v-${i.name}`} label={i.name} hint={`Different from “${i.example || "the example"}”`}><input id={`v-${i.name}`} value={verifyInputs[i.name.trim()] ?? ""} onChange={(e) => setVerifyInputs({ ...verifyInputs, [i.name.trim()]: e.target.value })} /></Field>))}</div>}
                {outcomes.length > 0 && <Field id="v-out" label="Should this second set end in a normal answer?" optional><select id="v-out" value={expectOutcome} onChange={(e) => setExpectOutcome(e.target.value)}><option value="">No, it should succeed</option>{outcomes.filter((x) => x.outcome.trim()).map((x) => <option key={x.outcome} value={x.outcome.trim()}>{x.outcome}</option>)}</select></Field>}
              </>)}
              <Field id="m-hint" label="Hint for the model" optional hint="Guidance when a first attempt got stuck."><textarea id="m-hint" rows={3} value={hint} onChange={(e) => setHint(e.target.value)} /></Field>
              <Field id="m-retry" label="An error banner worth retrying" optional hint="If this text appears, a run tries again (twice at most)."><input id="m-retry" value={retryText} onChange={(e) => setRetryText(e.target.value)} placeholder="APPLICATION ERROR" /></Field>
              <Field id="m-timeout" label="Text shown when the session has timed out" optional hint="A run then signs in again and carries on."><input id="m-timeout" value={timeoutText} onChange={(e) => setTimeoutText(e.target.value)} placeholder="SESSION TIMED OUT" /></Field>
            </div></details>
          <div className="row">
            <button className="btn primary" type="submit" disabled={busy || blocked}><IconBolt width={16} height={16} /> {busy ? "Starting…" : "Start discovery"}</button>
            {!retry && <button type="button" className="btn ghost" onClick={() => setStage("describe")}>Describe it again</button>}
            <span className="small muted">Up to {o.limits.max_steps} steps and {Math.round(o.limits.timeout_s / 60)} minutes. You can stop it at any time.</span>
          </div>
        </form>)}
    </div>
  );
}
export default function Page() { return <Suspense fallback={<div className="page"><Skeleton lines={6} /></div>}><NewTask /></Suspense>; }
