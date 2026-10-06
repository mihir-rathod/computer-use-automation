#!/usr/bin/env python
"""CLI entry point. See README.md for usage; these are the commands it implements.

    uv run python cli.py discover --capability mockbank.member_balance_lookup --param member_id=10001
    uv run python cli.py replay    --capability mockbank.member_balance_lookup --param member_id=10002

Both commands log in first (via the target's own login capability, replayed like any other
capability -- login is a first-class reusable capability, not special-cased CLI logic) and
write structured evidence -- a JSONL log of every perceive/act, screenshots, and the final
artifact or result -- to /evidence/<run>/ by default.

`replay` is a thin wrapper over runtime.run_replay() -- the same function the capability API
(api/app.py) calls -- so the CLI and the API can never diverge on how a capability actually
runs. `discover` stays CLI-only: the capability API is specifically about invoking
already-recorded capabilities, not about running discovery.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

import runtime
from agent.catalog import get_spec
from agent.commit_gate import auto_sandbox_gate, supervised_gate
from agent.gemini_client import GeminiClient
from agent.loop import DiscoveryLoop
from agent.recorder import build_artifact
from artifacts_lib import storage
from artifacts_lib.diff import diff_artifacts
from artifacts_lib.lint import has_errors, lint_artifact
from artifacts_lib.storage import load_artifact_by_id, save_artifact
from escalation.registry import register_session, unregister_session
from escalation.session_manager import SessionManager
from evidence_lib.logger import EvidenceLogger
from evidence_lib.redaction import Redactor
from safety.risk import RiskClassifier
from replay.result import ReplayStatus
from surface.web import WebSurface

EVIDENCE_ROOT = runtime.EVIDENCE_ROOT
TARGET_PROFILES = runtime.TARGET_PROFILES


def parse_params(pairs: list[str], input_schema: Any = None) -> dict[str, Any]:
    """`input_schema` (replay only -- pass the artifact's) coerces each value according to its
    *declared* type: replay's validate_input() correctly rejects a string against a "number" or
    "boolean" input_schema type. Coercion is schema-driven, not a blind "does this look
    numeric" heuristic -- member_id is deliberately a pattern-matched string, and guessing by
    shape alone would silently turn "10001" into an int (or worse, drop a leading zero from an
    id like "00123") wherever a value merely looks numeric. Discovery never passes a schema
    here: its own parameterization (agent/recorder.py's _parameterize) matches a parameter's
    literal value against recorded step text with `in`/`.replace()`, which requires a string.
    """
    params: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--param must be key=value, got: {pair!r}")
        key, value = pair.split("=", 1)
        declared_type = input_schema.properties.get(key, {}).get("type") if input_schema else None
        params[key] = _coerce_param(value, declared_type)
    return params


def _coerce_param(value: str, declared_type: Any) -> Any:
    types = declared_type if isinstance(declared_type, list) else [declared_type]
    if "boolean" in types:
        return value.lower() in ("true", "1", "yes")
    if "integer" in types:
        return int(value)
    if "number" in types:
        return float(value)
    return value


def cmd_discover(args: argparse.Namespace) -> int:
    load_dotenv()
    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit("GEMINI_API_KEY is not set -- see README.md Setup (needed for `discover`, not `replay`).")

    target = runtime.resolve_target(args.target, args.base_url, args.username, args.password, args.allowlist)
    spec = get_spec(args.capability, target["base_url"])
    params = parse_params(args.param)
    evidence_dir = Path(args.evidence_dir) if args.evidence_dir else EVIDENCE_ROOT / runtime.run_id("discovery_run")
    policy = runtime.default_policy()
    logger = EvidenceLogger(evidence_dir, redactor=Redactor.from_config(policy.redaction).with_secrets_from({**params, "password": target["password"]}))
    # Logged as soon as the evidence dir exists, before anything can go wrong -- the dashboard
    # needs to identify what a run *is* (capability, goal, target) even for a run that
    # crashes or hangs before ever reaching a terminal "discovery_result" event.
    logger.log("system", "run_start", kind="discovery", capability_id=spec.capability_id, goal=spec.goal, target=args.target)
    safety_policy = runtime.build_safety_policy(target["base_url"], target["allowlist"], policy)
    commit_gate = None
    if args.commit_mode == "supervised":
        commit_gate = supervised_gate()
    elif args.commit_mode == "auto-sandbox":
        commit_gate = auto_sandbox_gate(bool(target.get("sandbox")))

    if not args.no_operator_console:
        runtime.ensure_operator_console(args.operator_port)
    session = None

    # Not a `with sync_playwright() as p:` block on purpose -- same reasoning as
    # runtime.run_replay(): its __exit__ stops the driver unconditionally, which kills the
    # browser even if .close() is skipped (verified empirically). When --headed, a human is
    # watching specifically to review the final state, so the driver/browser are deliberately
    # left running for them to close themselves rather than vanishing the instant it's done.
    p = sync_playwright().start()
    browser = None
    try:
        browser = p.chromium.launch(headless=not args.headed, slow_mo=args.slow_mo)
        page = browser.new_page()
        page.set_default_timeout(runtime.DEFAULT_ACTION_TIMEOUT_MS)
        page.goto(f"{target['base_url']}{target['login_path']}")

        surface = WebSurface(page, base_url=target["base_url"], screenshot_dir=evidence_dir / "screenshots", evidence_logger=logger, safety_policy=safety_policy)
        # Discovering the target's own login capability is the one case where we must NOT log
        # in first -- that would be circular (you can't sign on before discovering how to sign
        # on). Every other capability assumes an authenticated session, hence the skip is keyed
        # off exactly this one id, not a general flag.
        if args.capability != target["login_capability"]:
            runtime.run_login(surface, target["username"], target["password"], login_capability=target["login_capability"])

        if not args.no_operator_console:
            session = SessionManager(evidence_dir.name, surface, evidence_dir, evidence_logger=logger, capability_id=spec.capability_id, goal=spec.goal)
            register_session(session)
            print(f"operator console (visit if this run pauses): http://127.0.0.1:{args.operator_port}/operator/{session.session_id}")

        loop = DiscoveryLoop(surface, GeminiClient(), evidence_logger=logger, max_steps=args.max_steps, timeout_seconds=args.timeout, session_manager=session, commit_gate=commit_gate, capability_id=spec.capability_id)
        derived = {*spec.success_output_defaults, *(r.output_field for r in spec.error_handling.business_outcomes)}
        result = loop.run(goal=spec.goal, parameters=params, start_path=spec.start_path,
                          output_names=[k for k in spec.output_schema.properties if k not in derived])
        if session is not None:
            unregister_session(session.session_id)
    finally:
        if not args.headed:
            if browser is not None:
                browser.close()
            p.stop()

    print(f"discovery stop_reason={result.stop_reason} steps={len(result.transcript)}" + (" (escalated to operator)" if result.escalated else ""))
    if result.reasoning:
        print(f"reasoning: {result.reasoning}")
    if result.stop_reason != "finished":
        print(f"evidence: {evidence_dir}")
        return 1

    existing = storage.list_versions(spec.capability_id)
    version = args.version or (storage.next_version(spec.capability_id) if existing else spec.version)
    artifact = build_artifact(
        result, params,
        capability_id=spec.capability_id, version=version, name=spec.name, description=spec.description,
        target=spec.target, preconditions=spec.preconditions,
        input_schema=spec.input_schema, output_schema=spec.output_schema,
        success_checkpoint=spec.success_checkpoint, error_handling=spec.error_handling,
        safety=spec.safety, success_output_defaults=spec.success_output_defaults,
        discovered_by=GeminiClient().model, discovery_run_id=evidence_dir.name,
        risk_classifier=RiskClassifier(policy.risk_keywords.commit, policy.risk_keywords.domain), canary=spec.canary,
    )
    saved_path = save_artifact(artifact, make_current=True if args.promote else None, by="discovery", reason=f"discovery run {evidence_dir.name}")
    (evidence_dir / "artifact.json").write_text(artifact.model_dump_json(indent=2))
    is_current = storage.current_version(spec.capability_id) == version
    print(f"saved artifact: {saved_path}")
    print(f"version {version} is {'now current' if is_current else 'a candidate (not current); review it, then: cli.py artifact promote ' + spec.capability_id + ' ' + version + ' --by YOU'}")
    print(f"evidence: {evidence_dir}")
    return 0


def _replay(args: argparse.Namespace, params: dict, evidence_dir: Path):
    return runtime.run_replay(
        args.capability, params,
        target=None if args.resume else args.target, base_url=args.base_url, username=args.username, password=args.password, allowlist=args.allowlist,
        headed=args.headed, slow_mo=args.slow_mo, evidence_dir=evidence_dir,
        operator_port=args.operator_port, enable_operator_console=not args.no_operator_console,
        dry_run=args.dry_run, deadline_s=args.timeout,
        idempotency_key=args.idempotency_key, requested_by=args.requested_by, resume_run_id=args.resume, repair_llm=args.repair_llm,
    )



def cmd_replay(args: argparse.Namespace) -> int:
    load_dotenv()
    if not args.resume and args.capability not in storage.capability_ids():
        print(f"error: unknown capability '{args.capability}'. known: {', '.join(storage.capability_ids())}")
        return 1
    artifact = load_artifact_by_id(args.capability) if not args.resume else None
    params = {} if args.resume else parse_params(args.param, input_schema=artifact.input_schema)
    # Computed here, not left to run_replay's own default, so the session id (== evidence_dir
    # name) is known up front -- the operator console URL can then be printed *before* the run
    # starts, matching discover's behavior, rather than only after it finishes.
    evidence_dir = Path(args.evidence_dir) if args.evidence_dir else EVIDENCE_ROOT / runtime.run_id("replay_run")

    if not args.no_operator_console:
        print(f"operator console (visit if this run pauses): http://127.0.0.1:{args.operator_port}/operator/{evidence_dir.name}")

    try:
        result, evidence_dir = _replay(args, params, evidence_dir)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}")
        return 1

    print(f"status: {result.status.value}")
    if result.run_id:
        print(f"run: {result.run_id}" + ("  (answered from an earlier run with the same idempotency key)" if result.deduplicated else ""))
    if result.status == ReplayStatus.PENDING_APPROVAL:
        print(f"needs a {result.approval_tier} approval: cli.py approve {result.run_id} --by NAME --reason WHY, then cli.py replay --capability {args.capability} --resume {result.run_id}")
    if result.outputs is not None:
        print("outputs:", json.dumps(result.outputs, indent=2))
    if result.business_outcome:
        print(f"business_outcome: {result.business_outcome}")
    if result.committed:
        print(f"committed: step {result.commit_step}")
    if result.repair_proposal_id:
        print(f"repair proposal: {result.repair_proposal_id}  (cli.py repair show {result.repair_proposal_id})")
    if result.error:
        print(f"error: {result.error.message}" + (f" (step {result.error.step_id})" if result.error.step_id else "")
              + (f" [{result.error.code}]" if result.error.code else ""))
    print(f"evidence: {evidence_dir}")
    return 1 if result.status in (ReplayStatus.HARD_FAILURE, ReplayStatus.NEEDS_REVIEW) else 0


def cmd_repair(args: argparse.Namespace) -> int:
    from repair.apply import approve_repair, reject_repair
    from repair.propose import RepairProposal
    from runs.store import RunError
    store, policy = runtime.default_store(), runtime.default_policy()
    try:
        if args.repair_command == "list":
            for r in store.list_repairs(args.status, args.capability):
                print(f"{r['id']}  {r['status']:<9} {r['capability_id']} {r['base_version']} step {r['step_id']}  confident={bool(r['confident'])}"
                      + (f"  -> {r['new_version']} by {r['decided_by']}" if r["new_version"] else ""))
        elif args.repair_command == "show":
            row = store.get_repair(args.proposal_id)
            if row is None:
                print(f"unknown proposal {args.proposal_id}")
                return 1
            p = RepairProposal.model_validate_json(row["proposal_json"])
            print(f"{p.id}  {row['status']}  {p.capability_id} {p.base_version} step {p.step_id}  method={p.method}\n{p.reason}")
            print(f"page: {p.page_url}\nscreenshot: {p.screenshot}")
            print("old locators:", [f"{l.strategy.value}:{l.value}" for l in p.old_target.locators])
            if p.new_target:
                print("new locators:", [f"{l.strategy.value}:{l.value}" for l in p.new_target.locators])
            for c in p.candidates:
                print(f"  candidate {c.ref}: {c.role} {c.name!r} score={c.score} {c.why}")
            if p.touches_irreversible_step:
                print("this step commits something: approval needs a supervisor")
        elif args.repair_command == "approve":
            done = approve_repair(store, policy, args.proposal_id, args.by, args.reason, promote=not args.no_promote)
            print(f"{done['capability_id']}: {done['from']} -> {done['to']} ({'now current' if done['promoted'] else 'candidate, not promoted'}); step {done['step_id']} changed")
        elif args.repair_command == "reject":
            reject_repair(store, args.proposal_id, args.by, args.reason)
            print(f"{args.proposal_id}: rejected by {args.by}")
    except RunError as exc:
        print(f"error: {exc}")
        return 1
    return 0


def cmd_canary(args: argparse.Namespace) -> int:
    import canary
    if args.canary_command == "history":
        for row in runtime.default_store().canary_history(args.capability, args.limit):
            print(f"{row['at']}  {'ok  ' if row['ok'] else 'FAIL'} {row['capability_id']} {row['version']}  {row['detail']}" + (f"  repair={row['repair_id']}" if row["repair_id"] else ""))
        return 0
    kw = {"target": args.target, "base_url": args.base_url}
    if args.canary_command == "schedule":
        canary.loop(args.every, **kw)
        return 0
    outcomes = canary.run_all(**kw)
    if not outcomes:
        print("no capability has a canary (a read-only capability with Artifact.canary set)")
    for o in outcomes:
        print(f"{'ok  ' if o.ok else 'FAIL'} {o.capability_id} {o.version}: {o.detail}" + (f"  (repair proposal {o.repair_proposal_id})" if o.repair_proposal_id else ""))
    return 0 if all(o.ok for o in outcomes) else 1


def cmd_metrics(args: argparse.Namespace) -> int:
    from runs import metrics as m
    data = m.compute(runtime.default_store(), args.hours, args.capability)
    pct = lambda v: "-" if v is None else f"{v * 100:.0f}%"  # noqa: E731
    sec = lambda v: "-" if v is None else f"{v:.1f}s"  # noqa: E731
    print(f"{'capability':<34}{'runs':>5} {'settled':>8} {'success':>8} {'failed':>7} {'review':>7} {'escal.':>7} {'p50':>7} {'p95':>7}")
    for c in data["capabilities"]:
        print(f"{c['capability_id']:<34}{c['runs']:>5} {c['settled']:>8} {pct(c['success_rate']):>8} {pct(c['failure_rate']):>7} {c['needs_review']:>7} "
              f"{pct(c['escalation_rate']):>7} {sec(c['latency_s']['p50']):>7} {sec(c['latency_s']['p95']):>7}")
    t = data["totals"]
    print(f"\npending approvals: {t['pending_approvals']}   pending repairs: {t['pending_repairs']}   failing canaries: {', '.join(t['failing_canaries']) or 'none'}")
    return 0


def cmd_keys(args: argparse.Namespace) -> int:
    from runs.store import RunError
    store = runtime.default_store()
    try:
        if args.keys_command == "create":
            key = store.create_key(args.name, args.role)
            print(f"API key for {args.name} ({args.role}). It is shown once and cannot be recovered:\n\n  {key}\n\nUse it as:  Authorization: Bearer {key}")
        elif args.keys_command == "list":
            for k in store.list_keys():
                print(f"{k['name']:<20} {k['role']:<11} created {k['created_at']}  last used {k['last_used_at'] or '-'}" + ("  REVOKED" if k["revoked_at"] else ""))
        elif args.keys_command == "revoke":
            n = store.revoke_key(args.name)
            print(f"revoked {n} key(s) named {args.name}")
    except RunError as exc:
        print(f"error: {exc}")
        return 1
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    from runs.approvals import decide_run, resolve_run
    from runs.store import RunError
    store, policy = runtime.default_store(), runtime.default_policy()
    cmd = args.runs_command
    try:
        if cmd == "list":
            for r in store.list_runs(args.capability, args.status, args.limit):
                print(f"{r['id']}  {r['status']:<17} {r['capability_id']}  by={r['requested_by']}  committed={bool(r['committed'])}  key={r['idempotency_key'] or '-'}")
        elif cmd == "show":
            run = store.get(args.run_id)
            if run is None:
                print(f"unknown run {args.run_id}")
                return 1
            run.pop("result_json", None)
            print(json.dumps({**run, "approvals": store.approvals_for(args.run_id)}, indent=2, default=str))
        elif cmd == "pending":
            for a in store.pending_approvals():
                print(f"{a['run_id']}  needs {a['tier']}  {a['capability_id']}  requested_by={a['requested_by']}  params={a['params_json']}")
        elif cmd in ("approve", "reject"):
            run = decide_run(store, policy, args.run_id, "approved" if cmd == "approve" else "rejected", args.by, args.reason)
            print(f"{args.run_id}: {run['status']} by {args.by}")
        elif cmd == "resolve":
            run = resolve_run(store, policy, args.run_id, args.outcome, args.by, args.reason)
            print(f"{args.run_id}: marked {args.outcome} by {args.by}")
    except RunError as exc:
        print(f"error: {exc}")
        return 1
    return 0


def cmd_artifact(args: argparse.Namespace) -> int:
    action = args.artifact_command
    if action == "list":
        for cid in storage.capability_ids():
            versions, current = storage.list_versions(cid), storage.current_version(cid)
            print(f"{cid}  current={current or 'none (a draft: not runnable until promoted)'}  versions={','.join(versions)}")
        return 0
    if action == "show":
        print(load_artifact_by_id(args.capability, version=args.version).model_dump_json(indent=2))
        return 0
    if action == "validate":
        ids = storage.capability_ids() if args.all else [args.capability]
        failed = False
        for cid in ids:
            # a draft (discovered, never promoted) has no current version: check its latest
            version = args.version or (None if storage.current_version(cid) else storage.list_versions(cid)[-1])
            artifact = load_artifact_by_id(cid, version=version)
            findings = lint_artifact(artifact)
            failed |= has_errors(findings)
            print(f"{cid} {artifact.version}: {'FAIL' if has_errors(findings) else 'ok'} ({len(findings)} finding(s))")
            for finding in findings:
                print(f"  {finding}")
        return 1 if failed else 0
    if action == "diff":
        diff = diff_artifacts(load_artifact_by_id(args.capability, version=args.version_a), load_artifact_by_id(args.capability, version=args.version_b))
        print(json.dumps(diff.to_dict(), indent=2) if args.json else diff.to_text())
        return 0
    if action in ("promote", "rollback"):
        # what production replays is decided here, so it needs someone on the roster, at the capability's own tier
        from safety.config import may_approve
        policy = runtime.default_policy()
        tier = policy.for_capability(args.capability).approval
        allowed, why = may_approve(policy, args.by, "operator" if tier == "live" else tier)
        if not allowed:
            print(f"error: {why}")
            return 1
    if action == "promote":
        findings = lint_artifact(load_artifact_by_id(args.capability, version=args.version))
        if has_errors(findings) and not args.force:
            print("refusing to promote: validation errors (fix them, or pass --force):")
            for finding in findings:
                print(f"  {finding}")
            return 1
        storage.set_current(args.capability, args.version, by=args.by, reason=args.reason)
        print(f"{args.capability}: current is now {args.version}")
        return 0
    if action == "rollback":
        print(f"{args.capability}: rolled back to {storage.rollback(args.capability, by=args.by, reason=args.reason)}")
        return 0
    if action == "history":
        for entry in storage.history(args.capability):
            print(f"{entry['at']}  {entry['action']:9} {entry['from'] or '-'} -> {entry['version']}  by {entry['by']}  {entry['reason']}")
        return 0
    return 2


def main() -> int:
    import observability
    observability.configure("WARNING")  # command output stays clean unless LOG_LEVEL is set
    parser = argparse.ArgumentParser(prog="cli.py")
    sub = parser.add_subparsers(dest="command", required=True)

    discover_p = sub.add_parser("discover", help="Run LLM-driven discovery and save the resulting artifact")
    discover_p.add_argument("--capability", required=True, help="capability_id from agent/catalog.py, e.g. mockbank.member_balance_lookup")
    discover_p.add_argument("--param", action="append", default=[], help="key=value, repeatable")
    discover_p.add_argument("--target", choices=sorted(TARGET_PROFILES), default="mockbank", help="which TARGET_PROFILES entry to use for base-url/credentials/allowlist/login")
    discover_p.add_argument("--base-url", default=None, help="override the --target profile's base_url")
    discover_p.add_argument("--username", default=None, help="override the --target profile's username")
    discover_p.add_argument("--password", default=None, help="override the --target profile's password")
    discover_p.add_argument("--allowlist", default=None, help="override the --target profile's allowlist JSON path")
    discover_p.add_argument("--headed", action="store_true", help="show the browser window instead of running headless")
    discover_p.add_argument("--slow-mo", type=int, default=0, help="milliseconds Playwright pauses before each action -- for watching a --headed run, e.g. 500-800")
    discover_p.add_argument("--evidence-dir", default=None)
    discover_p.add_argument("--version", default=None, help="semver for the saved artifact (default: the catalog's, or the next patch if one exists)")
    discover_p.add_argument("--promote", action="store_true", help="make the new version current immediately (default: only if it is the first)")
    discover_p.add_argument("--commit-mode", choices=["block", "supervised", "auto-sandbox"], default="block",
                            help="what to do when discovery reaches an irreversible step: block (default; it cannot commit), ask a person at the terminal, or auto-approve on a sandbox target")
    discover_p.add_argument("--max-steps", type=int, default=25)
    discover_p.add_argument("--timeout", type=int, default=300)
    discover_p.add_argument("--operator-port", type=int, default=8010)
    discover_p.add_argument("--no-operator-console", action="store_true", help="disable escalation -- a stuck run just fails instead of pausing for a human")
    discover_p.set_defaults(func=cmd_discover)

    replay_p = sub.add_parser("replay", help="Deterministically replay a saved artifact -- no LLM")
    replay_p.add_argument("--capability", required=True, help="capability_id of a saved artifact under /artifacts/")
    replay_p.add_argument("--param", action="append", default=[], help="key=value, repeatable")
    replay_p.add_argument("--operator-port", type=int, default=8010)
    replay_p.add_argument("--no-operator-console", action="store_true", help="disable escalation -- a hard failure just fails instead of pausing for a human")
    replay_p.add_argument("--target", choices=sorted(TARGET_PROFILES), default="mockbank", help="which TARGET_PROFILES entry to use for base-url/credentials/allowlist/login")
    replay_p.add_argument("--base-url", default=None, help="override the --target profile's base_url")
    replay_p.add_argument("--username", default=None, help="override the --target profile's username")
    replay_p.add_argument("--password", default=None, help="override the --target profile's password")
    replay_p.add_argument("--allowlist", default=None, help="override the --target profile's allowlist JSON path")
    replay_p.add_argument("--headed", action="store_true")
    replay_p.add_argument("--slow-mo", type=int, default=0, help="milliseconds Playwright pauses before each action -- for watching a --headed run, e.g. 500-800")
    replay_p.add_argument("--evidence-dir", default=None)
    replay_p.add_argument("--idempotency-key", default=None, help="a retry with the same key returns the first run's result instead of running again")
    replay_p.add_argument("--requested-by", default="cli", help="who is asking; recorded on the run and barred from approving it")
    replay_p.add_argument("--resume", default=None, metavar="RUN_ID", help="execute a run that has been approved (params come from the stored run)")
    replay_p.add_argument("--repair-llm", action="store_true", help="on an unresolved locator, let the model rank the candidate elements when the heuristic cannot (still only a proposal)")
    replay_p.add_argument("--dry-run", action="store_true", help="run every step up to, not including, the first irreversible one, then stop; nothing is committed")
    replay_p.add_argument("--timeout", type=float, default=runtime.DEFAULT_RUN_TIMEOUT_S, help="whole-run time budget in seconds")
    replay_p.set_defaults(func=cmd_replay)

    artifact_p = sub.add_parser("artifact", help="Inspect, validate, diff, promote and roll back saved artifacts")
    asub = artifact_p.add_subparsers(dest="artifact_command", required=True)
    asub.add_parser("list", help="every capability with its current and available versions")
    for name, text in (("show", "print one version as JSON"), ("validate", "lint an artifact for safety and reviewability problems")):
        cmd = asub.add_parser(name, help=text)
        cmd.add_argument("capability", nargs="?", default=None)
        cmd.add_argument("--version", default=None, help="default: the current version")
        if name == "validate":
            cmd.add_argument("--all", action="store_true", help="validate the current version of every capability")
    diff_cmd = asub.add_parser("diff", help="what changed between two versions")
    diff_cmd.add_argument("capability")
    diff_cmd.add_argument("version_a")
    diff_cmd.add_argument("version_b")
    diff_cmd.add_argument("--json", action="store_true")
    promote_cmd = asub.add_parser("promote", help="make a version current (refuses artifacts with validation errors)")
    promote_cmd.add_argument("capability")
    promote_cmd.add_argument("version")
    promote_cmd.add_argument("--by", required=True, help="who is promoting (recorded in the history)")
    promote_cmd.add_argument("--reason", default="")
    promote_cmd.add_argument("--force", action="store_true", help="promote despite validation errors")
    rollback_cmd = asub.add_parser("rollback", help="return to the version that was current before the last promotion")
    rollback_cmd.add_argument("capability")
    rollback_cmd.add_argument("--by", required=True)
    rollback_cmd.add_argument("--reason", default="")
    history_cmd = asub.add_parser("history", help="promotion and rollback history")
    history_cmd.add_argument("capability")
    artifact_p.set_defaults(func=cmd_artifact)

    repair_p = sub.add_parser("repair", help="Review and approve locator repair proposals produced by failed replays")
    psub = repair_p.add_subparsers(dest="repair_command", required=True)
    pl = psub.add_parser("list")
    pl.add_argument("--status", default=None, choices=["pending", "approved", "rejected"])
    pl.add_argument("--capability", default=None)
    ps = psub.add_parser("show")
    ps.add_argument("proposal_id")
    for name in ("approve", "reject"):
        pa = psub.add_parser(name)
        pa.add_argument("proposal_id")
        pa.add_argument("--by", required=True)
        pa.add_argument("--reason", required=True)
        if name == "approve":
            pa.add_argument("--no-promote", action="store_true", help="save the repaired version as a candidate instead of making it current")
    repair_p.set_defaults(func=cmd_repair)

    canary_p = sub.add_parser("canary", help="Scheduled read-only replays that catch UI drift early")
    csub = canary_p.add_subparsers(dest="canary_command", required=True)
    for name in ("run", "schedule"):
        cp = csub.add_parser(name)
        cp.add_argument("--target", choices=sorted(TARGET_PROFILES), required=True)
        cp.add_argument("--base-url", default=None)
        if name == "schedule":
            cp.add_argument("--every", type=float, default=900, help="seconds between rounds")
    ch = csub.add_parser("history")
    ch.add_argument("--capability", default=None)
    ch.add_argument("--limit", type=int, default=20)
    canary_p.set_defaults(func=cmd_canary)

    metrics_p = sub.add_parser("metrics", help="Per-capability success, failure, escalation and latency figures from the run store")
    metrics_p.add_argument("--hours", type=float, default=None, help="only runs created in the last N hours")
    metrics_p.add_argument("--capability", default=None)
    metrics_p.set_defaults(func=cmd_metrics)

    keys_p = sub.add_parser("keys", help="Create, list and revoke API keys (the key's name is the identity recorded on approvals)")
    ksub = keys_p.add_subparsers(dest="keys_command", required=True)
    kc = ksub.add_parser("create")
    kc.add_argument("--name", required=True)
    kc.add_argument("--role", required=True, choices=["viewer", "operator", "supervisor", "admin"])
    ksub.add_parser("list")
    kr = ksub.add_parser("revoke")
    kr.add_argument("--name", required=True)
    keys_p.set_defaults(func=cmd_keys)

    runs_p = sub.add_parser("runs", help="List and inspect recorded runs and pending approvals")
    rsub = runs_p.add_subparsers(dest="runs_command", required=True)
    rl = rsub.add_parser("list")
    rl.add_argument("--capability", default=None)
    rl.add_argument("--status", default=None)
    rl.add_argument("--limit", type=int, default=20)
    rsub.add_parser("pending", help="runs waiting for an approval")
    rs = rsub.add_parser("show")
    rs.add_argument("run_id")
    runs_p.set_defaults(func=cmd_runs)

    for name, helptext in (("approve", "Approve a run waiting for approval"), ("reject", "Reject a run waiting for approval")):
        ap = sub.add_parser(name, help=helptext)
        ap.add_argument("run_id")
        ap.add_argument("--by", required=True, help="approver name; must be on the roster in safety/policy.yaml at a high enough tier")
        ap.add_argument("--reason", required=True)
        ap.set_defaults(func=cmd_runs, runs_command=name)
    res = sub.add_parser("resolve", help="Settle a needs_review run after checking the target's own records")
    res.add_argument("run_id")
    res.add_argument("--outcome", required=True, choices=["committed", "not_committed"])
    res.add_argument("--by", required=True)
    res.add_argument("--reason", required=True)
    res.set_defaults(func=cmd_runs, runs_command="resolve")

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
