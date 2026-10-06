"""Discovery sessions: discovery driven from the console instead of the CLI, with the same safety rules.

One session runs at a time (the model's quota is shared and a person is watching it). A session:
  1. signs on with the target's own login capability, so the model never sees a password,
  2. lets the model drive the screens toward the goal in the contract, with the commit gate in place,
  3. records the transcript into an artifact and saves it as a DRAFT (no promoted version, so it cannot run),
  4. replays that draft once with a second set of inputs, to prove it works for more than the example,
  5. leaves the draft for a supervisor to review and promote. Nothing here promotes anything.
"""
from __future__ import annotations

import json
import logging
import re
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import observability
import runtime
from agent.commit_gate import CommitDecision, CommitGate, CommitRequest, auto_sandbox_gate
from agent.gemini_client import GeminiClient
from agent.loop import DiscoveryLoop
from agent.recorder import build_artifact
from artifacts_lib import storage
from artifacts_lib.lint import has_errors, lint_artifact
from evidence_lib.logger import EvidenceLogger
from evidence_lib.redaction import Redactor
from replay.engine import ReplayEngine
from replay.result import ReplayStatus
from runs.store import RunStore
from safety.config import PolicyConfig, irreversible_step_ids
from safety.risk import RiskClassifier
from surface.web import WebSurface
from artifacts_lib.schema import Signal, SignalType
from discover.contract import DiscoveryContract
from discover.derive import derive_success_text
from discover.prune import prune

log = logging.getLogger(__name__)
ACTIVE = ("queued", "running", "awaiting_commit", "verifying")


class DiscoveryError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def get_model() -> Any:
    """The model discovery uses. A function so tests can substitute a scripted one."""
    return GeminiClient()


def model_available() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def discoverable_targets(policy: PolicyConfig) -> list[dict[str, Any]]:
    """Targets a task can be discovered on. Sandboxes always; anything else only if the policy allows read-only discovery there."""
    out = []
    for name, profile in runtime.TARGET_PROFILES.items():
        sandbox = bool(profile.get("sandbox"))
        if sandbox or policy.discovery.non_sandbox_read_only:
            out.append({"name": name, "app_id": profile["app_id"], "base_url": profile["base_url"], "sandbox": sandbox, "read_only_only": not sandbox})
    return out


def check_contract(contract: DiscoveryContract, policy: PolicyConfig, adir: Path) -> dict[str, Any]:
    """Everything that can be refused before a browser is launched. Returns the target profile."""
    profile = runtime.TARGET_PROFILES.get(contract.target)
    if profile is None or contract.target not in {t["name"] for t in discoverable_targets(policy)}:
        raise DiscoveryError(f"'{contract.target}' is not a target tasks can be discovered on", 422)
    sandbox = bool(profile.get("sandbox"))
    if not sandbox and contract.effect != "read_only":
        raise DiscoveryError("on a system that is not a sandbox only read-only tasks can be discovered", 422)
    if contract.effect == "read_only" and contract.commit_approval == "supervisor":
        pass  # harmless: a read-only task is never offered a commit
    if contract.commit_approval == "auto_sandbox" and not sandbox and contract.effect != "read_only":
        raise DiscoveryError("automatic approval of the commit step is only available on a sandbox", 422)
    cid = contract.capability_id(profile["app_id"])
    if cid == profile["login_capability"]:
        raise DiscoveryError("the sign-on capability cannot be discovered here", 422)
    if cid in storage.capability_ids(adir) and storage.current_version(cid, adir) is not None:
        raise DiscoveryError(f"{cid} already exists and can be run. Pick a different task name; improving an existing task goes through repair", 409)
    return profile


class DiscoveryService:
    def __init__(self, store: RunStore, artifacts_dir: Path, policy: PolicyConfig):
        self.store, self.adir, self.policy = store, artifacts_dir, policy
        self._lock = threading.Lock()
        self._cancel: dict[str, threading.Event] = {}

    # ---- called from the API ---------------------------------------------------------------------------------------

    def start(self, contract: DiscoveryContract, who: str, retry_of: str | None = None) -> dict[str, Any]:
        if not model_available():
            raise DiscoveryError("no model key is configured on the server (GEMINI_API_KEY), so nothing can be discovered", 503)
        profile = check_contract(contract, self.policy, self.adir)
        with self._lock:
            active = self.store.discovery_active()
            if active:
                raise DiscoveryError(f"a discovery session is already running ({active[0]['capability_id']}, started by {active[0]['created_by']}). One at a time", 409)
            row = self.store.discovery_create(who, contract.capability_id(profile["app_id"]), contract.target, contract.model_dump_json(), retry_of)
            self._cancel[row["id"]] = threading.Event()
        observability.log("discover.queued", session_id=row["id"], capability_id=row["capability_id"], by=who)
        # a daemon thread: a session waiting for an answer from a person must never keep the server from shutting down
        threading.Thread(target=self._run_safely, args=(row["id"], contract), daemon=True, name="discover-session").start()
        return row

    def cancel(self, sid: str) -> None:
        row = self.store.discovery_get(sid)
        if row is None or row["status"] not in ACTIVE:
            raise DiscoveryError("that session is not running", 409)
        self._cancel.setdefault(sid, threading.Event()).set()

    def decide_commit(self, sid: str, approve: bool, who: str, reason: str) -> dict[str, Any]:
        from runs.store import ApprovalError
        try:
            return self.store.discovery_decide_commit(sid, "approved" if approve else "declined", who, reason)
        except ApprovalError as exc:
            raise DiscoveryError(str(exc), 409) from None

    def discard(self, sid: str) -> None:
        row = self.store.discovery_get(sid)
        if row is None:
            raise DiscoveryError("unknown session", 404)
        if row["status"] in ACTIVE:
            raise DiscoveryError("stop the session first", 409)
        if row["status"] == "promoted":
            raise DiscoveryError("this draft was promoted; roll it back from the task's page instead", 409)
        if row["version"]:
            storage.delete_unpublished_version(row["capability_id"], row["version"], self.adir)
        self.store.discovery_update(sid, status="discarded")

    def recover(self) -> None:
        """Sessions a dead process left active can never finish."""
        for row in self.store.discovery_active():
            self.store.discovery_update(sid=row["id"], status="failed", error="the server restarted while this session was running", finished_at=_now())

    # ---- the session itself ------------------------------------------------------------------------------------------

    def _run_safely(self, sid: str, contract: DiscoveryContract) -> None:
        try:
            self._run(sid, contract)
        except Exception as exc:  # noqa: BLE001 -- a session must always end in a state a person can read
            log.exception("discovery session crashed")
            self.store.discovery_update(sid, status="failed", error=_explain(exc), finished_at=_now())
            observability.log("discover.failed", level=logging.ERROR, session_id=sid)

    def _stopped(self, sid: str) -> bool:
        return self._cancel.get(sid, threading.Event()).is_set()

    def _run(self, sid: str, contract: DiscoveryContract) -> None:
        row = self.store.discovery_get(sid)
        assert row is not None
        profile = runtime.resolve_target(contract.target)
        cid = contract.capability_id(profile["app_id"])
        version = storage.next_version(cid, self.adir)
        spec = contract.to_spec(profile, version)
        evidence_dir = runtime.EVIDENCE_ROOT / ("discover_" + row["id"].removeprefix("disc_"))
        self.store.discovery_update(sid, status="running", version=version, evidence_dir=str(evidence_dir))
        params = contract.parameters()
        logger = EvidenceLogger(evidence_dir, redactor=Redactor.from_config(self.policy.redaction).with_secrets_from({**params, "password": profile["password"]}))
        logger.log("system", "run_start", kind="discovery", capability_id=cid, goal=contract.goal, target=contract.target, by=row["created_by"])
        safety_policy = runtime.build_safety_policy(profile["base_url"], profile["allowlist"], self.policy)
        gate = self._gate(sid, contract, profile, row["created_by"])
        model = get_model()
        outcome: dict[str, Any] = {}

        def job(page: Any) -> None:
            page.set_default_timeout(runtime.DEFAULT_ACTION_TIMEOUT_MS)
            page.goto(f"{profile['base_url']}{profile['login_path']}")
            surface = WebSurface(page, base_url=profile["base_url"], screenshot_dir=evidence_dir / "screenshots", evidence_logger=logger, safety_policy=safety_policy)
            _, login = runtime.try_login(surface, profile["username"], profile["password"], profile["login_capability"], self.adir)
            if login.status != ReplayStatus.SUCCESS:
                outcome["stop"] = "failed"
                outcome["error"] = f"signing on to {contract.target} failed ({(login.error.message if login.error else login.status.value)}), so nothing could be discovered"
                return
            loop = DiscoveryLoop(surface, model, evidence_logger=logger, max_steps=self.policy.discovery.max_steps, timeout_seconds=self.policy.discovery.timeout_s,
                                 commit_gate=gate, capability_id=cid, should_stop=lambda: self._stopped(sid), allow_navigate=False)
            derived = {*spec.success_output_defaults, *(r.output_field for r in spec.error_handling.business_outcomes)}
            result = loop.run(goal=spec.goal, parameters=params, start_path=spec.start_path, output_names=[k for k in spec.output_schema.properties if k not in derived])
            outcome["discovery"] = result
            if result.stop_reason != "finished":
                return
            result.transcript, dropped = prune(result.transcript, {o.name for o in contract.outputs})
            outcome["dropped"] = dropped
            read = {r.output_name for r in result.transcript if r.output_name and r.result.success}
            missing = [o.name for o in contract.outputs if o.name not in read]
            if missing:
                outcome["error"] = f"the model finished without reading: {', '.join(missing)}"
                outcome["attention"] = True
                return
            success = contract.success_text or derive_success_text(result.transcript, surface, [str(v) for v in params.values()])
            if success is None:
                outcome["error"] = "it could not decide how to recognise that the task worked. Say what the page shows when it has worked, under the details"
                outcome["attention"] = True
                return
            outcome["success_text"] = success
            outcome["success_decided"] = contract.success_text is None
            by_value = _reads_found_by_their_value(result.transcript)
            if by_value:
                outcome["error"] = by_value
                outcome["attention"] = True
                return
            artifact = build_artifact(
                result, params, capability_id=cid, version=version, name=spec.name, description=spec.description, target=spec.target,
                preconditions=spec.preconditions, input_schema=spec.input_schema, output_schema=spec.output_schema,
                success_checkpoint=Signal(type=SignalType.TEXT_PRESENT, value=success), error_handling=spec.error_handling, safety=spec.safety,
                success_output_defaults=spec.success_output_defaults, discovered_by=getattr(model, "model", "model"), discovery_run_id=evidence_dir.name,
                risk_classifier=RiskClassifier(self.policy.risk_keywords.commit, self.policy.risk_keywords.domain),
            )
            outcome["artifact"] = artifact
            verify_inputs = contract.verify_inputs()
            if verify_inputs is None:
                outcome["verify"] = {"ran": False}
                return
            self.store.discovery_update(sid, status="verifying")
            logger.log("system", "verification_start", inputs=verify_inputs)
            engine = ReplayEngine(surface, evidence_logger=logger, reauth_credentials={"username": profile["username"], "password": profile["password"]},
                                  artifacts_dir=self.adir, confirmed_steps=irreversible_step_ids(artifact), deadline_s=90)
            outcome["verify"] = _judge(contract, engine.run(artifact, verify_inputs))

        try:
            runtime._run_on_dedicated_thread(job, headed=False, slow_mo=0)  # type: ignore[arg-type]
        finally:
            logger.close()
        self._finish(sid, contract, cid, version, evidence_dir, outcome, row["created_by"])

    def _finish(self, sid: str, contract: DiscoveryContract, cid: str, version: str, evidence_dir: Path, outcome: dict[str, Any], who: str) -> None:
        result = outcome.get("discovery")
        steps = len(outcome["artifact"].steps) if "artifact" in outcome else (len(result.transcript) if result else None)
        common: dict[str, Any] = {"finished_at": _now(), "steps": steps, "stop_reason": result.stop_reason if result else None,
                                  "reasoning": result.reasoning if result else None}
        if "artifact" not in outcome:
            if outcome.get("stop") == "failed":
                status = "failed"
            elif outcome.get("attention"):
                status = "needs_attention"
            elif result is None:
                status = "failed"
            elif result.stop_reason == "cancelled":
                status = "cancelled"
            elif result.stop_reason == "error":
                status = "failed"
            else:
                status = "stuck"
            self.store.discovery_update(sid, status=status, error=outcome.get("error") or _stuck_message(result), **common)
            observability.log("discover.ended", session_id=sid, capability_id=cid, status=status)
            return
        artifact = outcome["artifact"]
        verify = outcome.get("verify") or {"ran": False}
        findings = lint_artifact(artifact)
        lint = [{"level": f.level, "code": f.code, "message": f.message, "step_id": f.step_id} for f in findings]
        note = f"Discovered from the console by {who} on {datetime.now(UTC).date()} from a {contract.target} session. "
        note += (("Replayed once with the example values, without the model. " if contract.verify and contract.verify.same_as_example else "Verified once with a second set of inputs. ") if verify.get("passed") else
                 "Not verified. " if not verify.get("ran") else "Verification FAILED; do not promote without reading why. ")
        if outcome.get("dropped"):
            note += f"{outcome['dropped']} exploratory or failed step(s) were left out of the recording. "
        if outcome.get("success_decided"):
            note += f"Success is recognised by the text “{outcome['success_text']}”, which the system chose from the page the answer was read from. "
        artifact.provenance.note = note + " Not yet human-reviewed."
        storage.save_artifact(artifact, self.adir, make_current=False, by=who, reason=f"discovered in session {sid}")
        (evidence_dir / "artifact.json").write_text(artifact.model_dump_json(indent=2))
        unused = sorted({re.search(r"input '([^']+)'", f.message).group(1) for f in findings if f.code == "unused-input" and re.search(r"input '([^']+)'", f.message)})
        if unused:
            status = "needs_attention"
            error = (f"the recording never uses the input {', '.join(repr(u) for u in unused)}: it would give the same answer whatever it is asked. "
                     "The model most likely found the answer by browsing instead of looking it up. Try again with a hint that names the function to use")
        elif contract.effect == "irreversible" and not irreversible_step_ids(artifact):
            status = "needs_attention"
            error = ("it was described as committing something, but no commit step was recorded: the model finished without taking the final step, "
                     "so running this would do nothing. Try again with a hint that says to press the final confirm button")
        elif has_errors(findings) or (verify.get("ran") and not verify.get("passed")):
            status = "needs_attention"
            error = "the recorded task failed its own check" if verify.get("ran") and not verify.get("passed") else "the recorded task has errors that must be fixed before it can run"
        else:
            status, error = "ready", None
        self.store.discovery_update(sid, status=status, error=error, verify_json=json.dumps(verify), lint_json=json.dumps(lint), **common)
        observability.log("discover.ended", session_id=sid, capability_id=cid, status=status)

    # ---- the commit gate -----------------------------------------------------------------------------------------------

    def _gate(self, sid: str, contract: DiscoveryContract, profile: dict[str, Any], who: str) -> CommitGate:
        if contract.effect == "read_only":
            def refuse(req: CommitRequest) -> CommitDecision:
                return CommitDecision(False, mode="supervised", note="this task was declared read-only")
            return refuse
        if contract.commit_approval == "auto_sandbox":
            return auto_sandbox_gate(bool(profile.get("sandbox")))
        wait_s = self.policy.discovery.commit_wait_s

        def ask(req: CommitRequest) -> CommitDecision:
            self.store.discovery_update(sid, status="awaiting_commit", commit_decision=None, commit_decided_by=None, commit_reason=None,
                                    commit_request_json=json.dumps({"description": req.description, "url": req.url, "page_title": req.page_title, "goal": req.goal, "at": _now()}))
            observability.log("discover.awaiting_commit", session_id=sid)
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline and not self._stopped(sid):
                row = self.store.discovery_get(sid) or {}
                if row.get("commit_decision"):
                    approved = row["commit_decision"] == "approved"
                    self.store.discovery_update(sid, status="running", commit_request_json=None)
                    return CommitDecision(approved, approver=row.get("commit_decided_by"), mode="supervised", note=row.get("commit_reason"))
                time.sleep(0.4)
            self.store.discovery_update(sid, status="running", commit_request_json=None)
            return CommitDecision(False, mode="supervised", note="cancelled" if self._stopped(sid) else f"nobody answered within {wait_s // 60} minutes")
        return ask


def _reads_found_by_their_value(transcript: list[Any]) -> str | None:
    """A step that finds the cell it reads by the text that cell showed last time ("Brennan, Avery") only works for that record. This catches a list or table where nothing
    beside the value says what it is, which the recorder cannot describe in a way that carries over to other records."""
    from artifacts_lib.schema import ActionType
    for r in transcript:
        if r.action.kind != ActionType.EXTRACT or not r.result.success or not r.result.extracted_value or r.result.resolved_target is None:
            continue
        value = str(r.result.extracted_value).strip()
        if len(value) >= 3 and any(value in loc.value for loc in r.result.resolved_target.locators):
            return (f"it reads '{r.output_name}' by looking for the text it saw last time (“{value}”), so it would only work for this one record. The value sits in a list or table "
                    "with nothing beside it that says what it is. Choose a task whose answer has a label next to it, or point it at a page that shows one record")
    return None


def _judge(contract: DiscoveryContract, result: Any) -> dict[str, Any]:
    v: dict[str, Any] = {"ran": True, "status": result.status.value, "business_outcome": result.business_outcome, "outputs": result.outputs,
                         "error": result.error.message if result.error else None, "warnings": [], "inputs": contract.verify_inputs()}
    expected = contract.verify.expect_outcome if contract.verify else None
    if expected:
        v["passed"] = result.status == ReplayStatus.BUSINESS_OUTCOME and result.business_outcome == expected
        v["expected"] = f"the normal answer '{expected}'"
    else:
        v["passed"] = result.status == ReplayStatus.SUCCESS
        v["expected"] = "success"
        if v["passed"]:
            empty = [o.name for o in contract.outputs if not (result.outputs or {}).get(o.name)]
            if empty:
                v["warnings"].append(f"returned nothing for: {', '.join(empty)}")
    return v


def _stuck_message(result: Any) -> str | None:
    if result is None:
        return None
    return {"give_up": "the model gave up", "dead_end": "the model kept repeating the same action", "max_steps": "the model ran out of steps",
            "timeout": "the model ran out of time", "cancelled": "stopped by a person", "error": result.reasoning or "an error stopped the model"}.get(result.stop_reason)


def _explain(exc: Exception) -> str:
    text = str(exc)
    if "ERR_CONNECTION_REFUSED" in text or "net::" in text:
        return "the target system could not be reached. Is it running?"
    return f"{type(exc).__name__}: {text[:300]}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
