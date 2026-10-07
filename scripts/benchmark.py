#!/usr/bin/env python
"""Measures the platform instead of describing it. Self-contained: it starts its own clinic (so nothing else needs to be running) and uses temporary state.

  replay     every clinic task under injected faults and UI drift, graded against the clinic's audit log (not against what a page said), plus how many model
             calls replay made (the claim is zero) and how long a replay takes.
  discovery  a model drafts and discovers read-only tasks from one sentence each (needs GEMINI_API_KEY); each recording is then replayed for other records and
             compared with the clinic's own data. Reports steps, time and tokens, set against replay, which costs none.

    uv run python scripts/benchmark.py replay [--trials 3]
    uv run python scripts/benchmark.py discovery [--trials 3]
    uv run python scripts/benchmark.py all

Results are printed as tables and saved to evidence/benchmark/. Read them with the sample sizes in mind: the numbers say how this system behaved on this
clinic, not how it will behave on a vendor's."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import uvicorn

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ["RUN_DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="cua-bench-")) / "bench.db")
os.environ.setdefault("LOG_LEVEL", "ERROR")

import observability  # noqa: E402
import runtime  # noqa: E402  (after RUN_DB_PATH)
from dotenv import load_dotenv  # noqa: E402
from repair.apply import approve_repair  # noqa: E402
from repair.propose import RepairProposal  # noqa: E402
from replay.result import ReplayStatus  # noqa: E402
from runs.approvals import decide_run  # noqa: E402
from safety.config import irreversible_step_ids  # noqa: E402

load_dotenv()
observability.configure("ERROR")  # the run logs would drown the tables
BASE = ""  # set when the clinic starts

# ---- the clinic ------------------------------------------------------------------------------------------------------------------------------------

def start_clinic() -> str:
    from clinic.app import create_app
    from clinic.settings import Settings

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(Settings()), host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(f"{base}/legacy/login", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    for name in ("clinic", "clinic_supervisor"):
        runtime.TARGET_PROFILES[name]["base_url"] = base
    return base


_TRIAL_DBS = {"n": 0}


def reset(fresh_db: bool = True) -> None:
    # A fresh run database each time. The policy caps commits per task per day (25), so one shared database would refuse the later trials before any browser opens:
    # correct behaviour for the platform, but it would measure the cap, not the fault.
    if fresh_db:
        _TRIAL_DBS["n"] += 1
        os.environ["RUN_DB_PATH"] = str(Path(tempfile.gettempdir()) / f"cua-bench-trial-{os.getpid()}-{_TRIAL_DBS['n']}.db")
    httpx.post(f"{BASE}/_test/reset", timeout=10)
    httpx.delete(f"{BASE}/_test/chaos", timeout=10)
    httpx.delete(f"{BASE}/_test/drift", timeout=10)
    httpx.post(f"{BASE}/_test/chaos/duplicate-guard", json={"enabled": False}, timeout=10)  # the platform must not depend on the target to stop a double post


def chaos(kind: str, **kw: Any) -> None:
    httpx.post(f"{BASE}/_test/chaos", json={"kind": kind, **kw}, timeout=10).raise_for_status()


def effects(action: str) -> int:
    return len(httpx.get(f"{BASE}/_test/audit", params={"action": action, "effects_only": True}, timeout=10).json()["items"])


def artifacts_copy(tmp: Path, name: str) -> Path:
    out = tmp / name
    shutil.copytree(REPO / "artifacts", out)
    return out

# ---- replay: faults -----------------------------------------------------------------------------------------------------------------------------

CASES = {
    "clinic.patient_lookup": ("clinic", {"mrn": "LK-100002"}),
    "clinic.update_patient_contact": ("clinic", {"mrn": "LK-100002", "phone": "206-555-0142", "email": "x.y@example.test", "address": "9 Fir Ave, Oakridge, WA 98021"}),
    "clinic.reschedule_appointment": ("clinic", {"appointment": "A-20003", "date": "2026-03-12", "time": "11:30"}),
    "clinic.cancel_appointment": ("clinic", {"appointment": "A-20002", "reason": "patient_request"}),
    "clinic.issue_refund": ("clinic", {"invoice": "INV-30001", "amount": "25.00", "reason": "duplicate_payment"}),
    "clinic.submit_claim": ("clinic", {"invoice": "INV-30002", "amount": "124.00"}),
    "clinic.write_off_balance": ("clinic_supervisor", {"invoice": "INV-30002", "amount": "40.00", "reason": "uncollectible"}),
}
EFFECT = {"clinic.cancel_appointment": "appointment.cancel", "clinic.issue_refund": "refund.issue", "clinic.submit_claim": "claim.submit",
          "clinic.write_off_balance": "writeoff.apply", "clinic.update_patient_contact": "patient.update_contact", "clinic.reschedule_appointment": "appointment.reschedule"}
FIRST_PAGE = {"clinic.patient_lookup": "/legacy/patients*", "clinic.update_patient_contact": "/legacy/patients*"}  # the first content page after sign-on; the rest open /legacy/fn/*
COMMIT_PATH = {"clinic.update_patient_contact": "/legacy/patients/*/contact", "clinic.reschedule_appointment": "/legacy/appointments/*/reschedule"}  # the rest confirm here:
DEFAULT_COMMIT_PATH = "/legacy/transactions/confirm"

# (name, what it does, applies to, what a good platform does)
CONDITIONS = [
    ("baseline", "no fault", "all", "success"),
    ("slow system", "every page takes 600 ms longer", "all", "success"),
    ("transient server error", "the first page errors once (HTTP 500)", "all", "success"),
    ("session expires", "the session is lost on the first page", "all", "success"),
    ("maintenance page", "the first page shows a maintenance notice once", "all", "safe_stop"),
    ("error after commit", "the commit is applied, then the reply is a 500", "commits", "safe_stop"),
    ("error before commit", "the commit request errors before anything is applied", "commits", "safe_stop"),
    ("session expires at commit", "the session is lost as the commit is sent", "commits", "safe_stop"),
]


def first_page(cap: str) -> str:
    return FIRST_PAGE.get(cap, "/legacy/fn/*")


def inject(cond: str, cap: str) -> None:
    commit = COMMIT_PATH.get(cap, DEFAULT_COMMIT_PATH)
    if cond == "slow system":
        chaos("latency", params={"ms": 600}, method="GET", path_glob="/legacy/*", remaining=None)
    elif cond == "transient server error":
        chaos("error500", method="GET", path_glob=first_page(cap), remaining=1)
    elif cond == "session expires":
        chaos("expire_session", method="GET", path_glob=first_page(cap), remaining=1)
    elif cond == "maintenance page":
        chaos("maintenance", method="GET", path_glob=first_page(cap), remaining=1)
    elif cond == "error after commit":
        chaos("error500_after", method="POST", path_glob=commit, remaining=1)
    elif cond == "error before commit":
        chaos("error500", method="POST", path_glob=commit, remaining=1)
    elif cond == "session expires at commit":
        chaos("expire_session", method="POST", path_glob=commit, remaining=1)


def run_one(cap: str, arts: Path, tmp: Path, tag: str):
    target, params = CASES[cap]
    store, policy = runtime.default_store(), runtime.default_policy()
    kw = dict(target=target, base_url=BASE, enable_operator_console=False, artifacts_dir=arts, requested_by="benchmark")
    res, _ = runtime.run_replay(cap, params, evidence_dir=tmp / f"ev-{tag}", **kw)
    if res.status == ReplayStatus.PENDING_APPROVAL:  # a commit waits for a person; here, a supervisor approves it
        decide_run(store, policy, res.run_id, "approved", "suzie.visor", "benchmark")
        res, _ = runtime.run_replay(cap, {}, evidence_dir=tmp / f"ev-{tag}b", **{**kw, "target": None, "resume_run_id": res.run_id})
    return res


def grade(cap: str, res: Any, state_changes: int) -> str:
    """correct | safe_stop | unsafe. Judged on what the clinic recorded: a task changes state exactly once if it commits and never if it only reads."""
    allowed = 1 if cap in EFFECT else 0
    if state_changes > allowed:
        return "unsafe"  # a double post
    if res.status in (ReplayStatus.SUCCESS, ReplayStatus.BUSINESS_OUTCOME):
        return "correct" if state_changes == allowed else "unsafe"  # it said it worked but the record does not agree
    return "safe_stop"  # it stopped, did not repeat a commit, and left no more than one change


def fault_fired() -> bool:
    return bool(httpx.get(f"{BASE}/_test/chaos", timeout=10).json().get("log"))


def bench_faults(trials: int, tmp: Path) -> list[dict[str, Any]]:
    from artifacts_lib.storage import load_artifact_by_id

    rows = []
    arts = artifacts_copy(tmp, "faults")
    irreversible = {cap: bool(irreversible_step_ids(load_artifact_by_id(cap, arts))) for cap in CASES}
    for cond, _what, applies, _expect in CONDITIONS:
        for cap in CASES:
            if applies == "commits" and cap not in EFFECT:
                continue
            for t in range(trials):
                reset()
                inject(cond, cap)
                try:
                    res = run_one(cap, arts, tmp, f"{cond.replace(' ', '_')}-{cap.split('.')[-1]}-{t}")
                    changes = effects(EFFECT[cap]) if cap in EFFECT else 0
                    verdict = grade(cap, res, changes)
                    fired = cond == "baseline" or fault_fired()
                    seconds = (res.finished_at - res.started_at).total_seconds()
                    code = res.error.code if res.error else None
                    status = res.status.value
                except Exception as exc:  # noqa: BLE001 -- a crash is a result too
                    verdict, changes, seconds, code, status, fired = "unsafe", -1, 0.0, f"crash: {type(exc).__name__}", "crash", True
                rows.append({"condition": cond, "capability": cap, "trial": t, "verdict": verdict, "status": status, "error_code": code, "state_changes": changes, "seconds": seconds,
                             "fired": fired, "irreversible": irreversible[cap]})
                print(f"  {cond:<26} {cap.split('.')[-1]:<22} {t}  {verdict:<9} {status}{' / ' + code if code else ''}", flush=True)
    return rows


def faults_table(rows: list[dict[str, Any]]) -> str:
    out = ["| Fault | What happens | A good platform | Runs | As expected | Safe stops | Unsafe |", "|---|---|---|---|---|---|---|"]
    for cond, what, applies, expect in CONDITIONS:
        mine = [r for r in rows if r["condition"] == cond and r["fired"]]
        if not mine:
            continue
        stops = sum(r["verdict"] == "safe_stop" for r in mine)
        unsafe = sum(r["verdict"] == "unsafe" for r in mine)

        def ok(r: dict[str, Any]) -> bool:
            if expect == "success":
                return r["verdict"] == "correct"
            if applies == "commits" and not r["irreversible"]:
                return r["verdict"] in ("correct", "safe_stop")  # a reversible step may be retried; it must still change state at most once
            return r["verdict"] == "safe_stop"  # an irreversible step must never be retried on a guess

        met = sum(ok(r) for r in mine)
        skipped = sum(1 for r in rows if r["condition"] == cond and not r["fired"])
        out.append(f"| {cond} | {what} | {'carries on' if expect == 'success' else 'stops safely (irreversible), or at most one change (reversible)' if applies == 'commits' else 'stops safely'} | "
                   f"{len(mine)}{f' (+{skipped} not injected)' if skipped else ''} | {met}/{len(mine)} ({100 * met // len(mine)}%) | {stops} | {unsafe} |")
    return "\n".join(out)

# ---- replay: drift and repair --------------------------------------------------------------------------------------------------------------------

MAX_ROUNDS = 14


def drift_trial(cap: str, level: int, seed: str, tmp: Path) -> dict[str, Any]:
    target, params = CASES[cap]
    reset()
    httpx.post(f"{BASE}/_test/drift", json={"level": level, "seed": seed}, timeout=10)
    arts = artifacts_copy(tmp, f"drift-{cap.split('.')[-1]}-{level}-{seed}")
    store, policy = runtime.default_store(), runtime.default_policy()
    row = {"capability": cap, "level": level, "seed": seed, "proposals": 0, "confident": 0, "accurate": 0, "outcome": "?", "state_changes": None}
    pending: list[str] = []  # step ids of proposals approved, to be checked against the next failure
    for r in range(MAX_ROUNDS):
        res, _ = runtime.run_replay(cap, params, target=target, base_url=BASE, enable_operator_console=False, artifacts_dir=arts, requested_by="benchmark",
                                    evidence_dir=tmp / f"ev-d-{cap}-{level}-{seed}-{r}")
        if res.status == ReplayStatus.PENDING_APPROVAL:
            decide_run(store, policy, res.run_id, "approved", "suzie.visor", "benchmark")
            res, _ = runtime.run_replay(cap, {}, target=None, base_url=BASE, enable_operator_console=False, artifacts_dir=arts, resume_run_id=res.run_id,
                                        evidence_dir=tmp / f"ev-d-{cap}-{level}-{seed}-{r}b")
        failed_at = f"{(res.error.step_id if res.error else None)}"
        # a proposal was accurate if, after approving it, the run got past the step it repaired (it failed somewhere else, or finished)
        row["accurate"] += sum(1 for s in pending if s != failed_at)
        pending = []
        if res.status in (ReplayStatus.SUCCESS, ReplayStatus.BUSINESS_OUTCOME):
            row["outcome"] = "recovered" if row["proposals"] else "unaffected"
            break
        if not res.repair_proposal_id:
            row["outcome"] = f"stuck ({res.error.code if res.error else res.status.value})"
            break
        proposal = RepairProposal.model_validate_json(store.get_repair(res.repair_proposal_id)["proposal_json"])
        row["proposals"] += 1
        if not proposal.confident:
            row["outcome"] = "no confident match"
            break
        row["confident"] += 1
        pending.append(f"{proposal.step_id}")
        approve_repair(store, policy, proposal.id, "suzie.visor", "benchmark", arts)
    else:
        row["outcome"] = "gave up"
    row["state_changes"] = effects(EFFECT[cap]) if cap in EFFECT else 0
    httpx.delete(f"{BASE}/_test/drift", timeout=10)
    return row


def bench_drift(trials: int, tmp: Path) -> list[dict[str, Any]]:
    rows = []
    for level in (1, 2, 3):
        for cap in CASES:
            for t in range(trials):
                r = drift_trial(cap, level, f"b{level}{t}", tmp)
                rows.append(r)
                print(f"  drift {level}  {cap.split('.')[-1]:<22} {r['outcome']:<18} proposals={r['proposals']} changes={r['state_changes']}", flush=True)
    return rows


def drift_table(rows: list[dict[str, Any]]) -> str:
    names = {1: "ids and classes renamed", 2: "+ button, link and heading labels", 3: "+ form field names"}
    out = ["| Drift | What changes | Runs | Recovered | Unaffected | Failed | Proposals per task (median / max) | Proposals that were right | Wrong state changes |", "|---|---|---|---|---|---|---|---|---|"]
    for level in (1, 2, 3):
        mine = [r for r in rows if r["level"] == level]
        if not mine:
            continue
        rec = sum(r["outcome"] == "recovered" for r in mine)
        un = sum(r["outcome"] == "unaffected" for r in mine)
        bad = len(mine) - rec - un
        props = [r["proposals"] for r in mine if r["proposals"]]
        total_p = sum(r["proposals"] for r in mine)
        right = sum(r["accurate"] for r in mine)
        wrong_state = sum(1 for r in mine if (r["state_changes"] or 0) > 1)
        out.append(f"| {level} | {names[level]} | {len(mine)} | {rec} | {un} | {bad} | {int(statistics.median(props)) if props else 0} / {max(props) if props else 0} | "
                   f"{right}/{total_p}{f' ({100 * right // total_p}%)' if total_p else ''} | {wrong_state} |")
    return "\n".join(out)


def latency_summary(rows: list[dict[str, Any]]) -> dict[str, float]:
    s = sorted(r["seconds"] for r in rows if r["condition"] == "baseline" and r["verdict"] == "correct")
    return {"n": len(s), "p50": statistics.median(s), "p95": s[min(len(s) - 1, int(0.95 * len(s)))]} if s else {}


def run_replay_bench(trials: int) -> dict[str, Any]:
    from agent.gemini_client import GeminiClient

    calls = {"n": 0}
    real = GeminiClient.generate

    def counted(self, *a, **k):  # replay must never reach the model; this counts it if it does
        calls["n"] += 1
        return real(self, *a, **k)

    GeminiClient.generate = counted  # type: ignore[method-assign]
    tmp = Path(tempfile.mkdtemp(prefix="cua-bench-run-"))
    print("Faults:", flush=True)
    faults = bench_faults(trials, tmp)
    print("Drift:", flush=True)
    drift = bench_drift(max(1, trials // 2), tmp)
    shutil.rmtree(tmp, ignore_errors=True)
    total = len(faults) + len(drift)
    return {"faults": faults, "drift": drift, "model_calls_during_replay": calls["n"], "replay_runs": total, "baseline_latency_s": latency_summary(faults)}

# ---- discovery -----------------------------------------------------------------------------------------------------------------------------------

class Oracle:
    """The clinic's own data, read straight from the domain layer, independent of anything the recording did."""

    def __init__(self) -> None:
        from tests.clinic.conftest import World
        self.w = World()
        self.a = self.w.actor()

    def patient(self, mrn: str) -> dict[str, Any] | None:
        try:
            return self.w.clinic.patient_by_mrn(mrn)
        except Exception:  # noqa: BLE001
            return None


def _tasks(o: Oracle) -> list[dict[str, Any]]:
    def patient_values(mrn: str):
        p = o.patient(mrn)
        return None if p is None else [p["last_name"], "".join(ch for ch in p["phone"] if ch.isdigit())]

    def appointment_values(number: str):
        try:
            a = o.w.clinic.appointment_info(number)
        except Exception:  # noqa: BLE001
            return None
        return [a["patient"]["last_name"], a["provider"]]

    def invoice_values(number: str):
        try:
            i = o.w.clinic.invoice_info(number)
        except Exception:  # noqa: BLE001
            return None
        return [i["patient"]["last_name"], f"{i['balance_cents'] / 100:,.2f}"]

    def schedule_values(day: str):
        rows = o.w.clinic.schedule(o.a, day, "")
        return [rows[0]["last_name"], rows[0]["provider"]] if rows else None

    return [
        {"id": "patient_contact", "request": "Look up patient LK-100002 and tell me their last name and their phone number", "example": "LK-100002",
         "holdout": [f"LK-1000{n:02d}" for n in range(1, 16) if n != 2], "expected": patient_values},
        {"id": "appointment_details", "request": "Look up appointment A-20002 and tell me the patient and the provider", "example": "A-20002",
         "holdout": [f"A-200{n:02d}" for n in range(1, 16) if n != 2], "expected": appointment_values},
        {"id": "invoice_balance", "request": "What is the balance on invoice INV-30002? Tell me the patient it belongs to and the balance", "example": "INV-30002",
         "holdout": [f"INV-300{n:02d}" for n in range(1, 16) if n != 2], "expected": invoice_values},
        {"id": "schedule_first", "request": "Who has the first appointment on 2026-03-04 and which provider is it with", "example": "2026-03-04",
         "holdout": [f"2026-03-{d:02d}" for d in range(3, 20) if d != 4], "expected": schedule_values},
    ]


class Meter:
    """Wraps the model so every call's tokens are counted."""

    def __init__(self, inner: Any):
        self.inner, self.model = inner, inner.model
        self.calls = self.prompt = self.output = 0

    def generate(self, *a: Any, **k: Any) -> Any:
        r = self.inner.generate(*a, **k)
        self.calls += 1
        u = getattr(r, "usage_metadata", None)
        if u is not None:
            self.prompt += int(getattr(u, "prompt_token_count", 0) or 0)
            self.output += int(getattr(u, "candidates_token_count", 0) or 0)
        return r


def _norm(s: Any) -> str:
    return str(s).lower()


def _matches(outputs: dict[str, Any], expected: list[str]) -> bool:
    values = [_norm(v) for k, v in outputs.items() if k != "status" and v is not None]
    digits = ["".join(ch for ch in v if ch.isdigit()) for v in values]
    for want in expected:
        w = _norm(want)
        if not (any(w in v for v in values) or (w.isdigit() and any(w in d for d in digits))):
            return False
    return True


def discover_trial(task: dict[str, Any], tmp: Path, svc: Any, arts: Path) -> dict[str, Any]:
    import discover.service as ds
    from artifacts_lib import storage
    from discover.draft import draft_contract
    from discover.peek import peek_home

    reset(fresh_db=False)  # discovery tracks its sessions in one store, and it only reads, so the commit cap does not matter here
    ds._real_get_model = getattr(ds, "_real_get_model", ds.get_model)  # keep the real one, so each trial wraps it afresh
    meter = Meter(ds._real_get_model())
    ds.get_model = lambda: meter  # type: ignore[assignment]
    profile = runtime.TARGET_PROFILES["clinic"]
    started = time.monotonic()
    row: dict[str, Any] = {"task": task["id"], "status": "?", "steps": None, "recorded": None, "accuracy": None, "holdout": 0}
    try:
        drafted = draft_contract(meter, task["request"], "clinic", profile, menu=peek_home(profile, runtime.default_policy(), arts))
        if "contract" not in drafted:
            row["status"] = "asked a question instead of drafting"
            return row
        contract = drafted["contract"]
        contract["task_name"] = f"{contract['task_name']}_{int(time.time())}"[:40]
        from discover.contract import DiscoveryContract
        session = svc.start(DiscoveryContract.model_validate(contract), "benchmark")
        end = time.monotonic() + 420
        while time.monotonic() < end:
            cur = runtime.default_store().discovery_get(session["id"])
            if cur["status"] not in ("queued", "running", "awaiting_commit", "verifying"):
                break
            time.sleep(1)
        row["status"] = cur["status"]
        row["discovery_s"] = round(time.monotonic() - started, 1)
        row["prompt_tokens"], row["output_tokens"], row["model_calls"] = meter.prompt, meter.output, meter.calls
        row["steps"] = cur.get("steps")
        row["error"] = cur.get("error")
        row["reasoning"] = cur.get("reasoning")
        row["verify"] = cur.get("verify_json")
        if cur["status"] != "ready":
            return row
        cid, version = cur["capability_id"], cur["version"]
        art = storage.load_artifact_by_id(cid, arts, version=version)
        row["recorded"] = len(art.steps)
        storage.set_current(cid, version, arts, by="benchmark", reason="benchmark")
        name = contract["inputs"][0]["name"]
        good = total = 0
        seconds = []
        for value in task["holdout"]:
            want = task["expected"](value)
            if want is None:
                continue
            total += 1
            res, _ = runtime.run_replay(cid, {name: value}, target="clinic", base_url=BASE, enable_operator_console=False, artifacts_dir=arts,
                                        requested_by="benchmark", evidence_dir=tmp / f"ev-h-{task['id']}-{value}")
            seconds.append((res.finished_at - res.started_at).total_seconds())
            if res.status == ReplayStatus.SUCCESS and _matches(res.outputs or {}, want):
                good += 1
            if total >= 10:
                break
        row["holdout"], row["accuracy"] = total, good
        row["replay_s"] = round(statistics.mean(seconds), 2) if seconds else None
    except Exception as exc:  # noqa: BLE001
        row["status"] = f"crash: {type(exc).__name__}: {str(exc)[:80]}"
    return row


def run_discovery_bench(trials: int, only: list[str] | None = None) -> dict[str, Any]:
    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit("discovery needs GEMINI_API_KEY (set it in .env)")
    from discover.service import DiscoveryService

    tmp = Path(tempfile.mkdtemp(prefix="cua-bench-disc-"))
    arts = artifacts_copy(tmp, "arts")
    os.environ["ARTIFACTS_DIR"] = str(arts)
    svc = DiscoveryService(runtime.default_store(), arts, runtime.default_policy())
    oracle = Oracle()
    rows = []
    for task in _tasks(oracle):
        if only and task["id"] not in only:
            continue
        for t in range(trials):
            r = discover_trial(task, tmp, svc, arts)
            rows.append(r)
            print(f"  {task['id']:<20} #{t}  {r['status']:<16} steps={r.get('steps')} recorded={r.get('recorded')} accuracy={r.get('accuracy')}/{r.get('holdout')} "
                  f"tokens={(r.get('prompt_tokens') or 0) + (r.get('output_tokens') or 0)} time={r.get('discovery_s')}s", flush=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return {"discovery": rows}


def discovery_table(rows: list[dict[str, Any]]) -> str:
    out = ["| Task | Attempts | Ready | Steps explored (mean) | Steps recorded | Time | Tokens (in / out) | Right on other records | Replay |", "|---|---|---|---|---|---|---|---|---|"]
    for task in dict.fromkeys(r["task"] for r in rows):
        mine = [r for r in rows if r["task"] == task]
        ready = [r for r in mine if r["status"] == "ready"]
        m = lambda key: statistics.mean([r[key] for r in ready if r.get(key) is not None]) if any(r.get(key) is not None for r in ready) else 0  # noqa: E731
        good, tot = sum(r["accuracy"] or 0 for r in ready), sum(r["holdout"] for r in ready)
        out.append(f"| {task} | {len(mine)} | {len(ready)} | {m('steps'):.1f} | {m('recorded'):.1f} | {m('discovery_s'):.0f} s | {m('prompt_tokens'):,.0f} / {m('output_tokens'):,.0f} | "
                   f"{good}/{tot}{f' ({100 * good // tot}%)' if tot else ''} | {m('replay_s'):.1f} s, 0 tokens |")
    return "\n".join(out)

# ---- main ----------------------------------------------------------------------------------------------------------------------------------------

def main() -> None:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["replay", "discovery", "all"])
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--tasks", nargs="*", help="discovery: only these task ids")
    args = ap.parse_args()
    BASE = start_clinic()
    out: dict[str, Any] = {"when": datetime.now(UTC).isoformat(timespec="seconds"), "trials": args.trials}
    if args.what in ("replay", "all"):
        out["replay"] = run_replay_bench(args.trials)
        r = out["replay"]
        print("\n## Faults\n\n" + faults_table(r["faults"]))
        print("\n## UI drift and repair\n\n" + drift_table(r["drift"]))
        print(f"\nModel calls made during {r['replay_runs']} replays: {r['model_calls_during_replay']}. Replay time (no fault): "
              f"median {r['baseline_latency_s'].get('p50', 0):.1f} s, p95 {r['baseline_latency_s'].get('p95', 0):.1f} s over {r['baseline_latency_s'].get('n', 0)} runs.")
    if args.what in ("discovery", "all"):
        out["discovery"] = run_discovery_bench(args.trials, args.tasks)
        print("\n## Discovery\n\n" + discovery_table(out["discovery"]["discovery"]))
    dest = REPO / "evidence" / "benchmark"
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{args.what}.json"
    path.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nSaved {path.relative_to(REPO)}")


if __name__ == "__main__":
    main()
