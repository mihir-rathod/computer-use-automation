#!/usr/bin/env python
"""CLI entry point. See README.md's "Demo path" for the exact commands this implements.

    uv run python cli.py discover --capability mockbank.member_balance_lookup --param member_id=10001
    uv run python cli.py replay    --capability mockbank.member_balance_lookup --param member_id=10002

Both commands log in first (via the target's own login capability, replayed like any other
capability -- login is a first-class reusable capability, not special-cased CLI logic) and
write structured evidence -- a JSONL log of every perceive/act, screenshots, and the final
artifact or result -- to /evidence/<run>/ by default.

`replay` is a thin wrapper over runtime.run_replay() -- the same function the capability API
(api/app.py) calls -- so the CLI and the API can never diverge on how a capability actually
runs. `discover` stays CLI-only: the brief's capability API is specifically about invoking
already-recorded capabilities (3.2), not about running discovery.
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
from agent.gemini_client import GeminiClient
from agent.loop import DiscoveryLoop
from agent.recorder import build_artifact
from artifacts_lib.storage import load_artifact_by_id, save_artifact
from escalation.registry import register_session, unregister_session
from escalation.session_manager import SessionManager
from evidence_lib.logger import EvidenceLogger
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
    logger = EvidenceLogger(evidence_dir)
    # Logged as soon as the evidence dir exists, before anything can go wrong -- the dashboard
    # (Phase 5) needs to identify what a run *is* (capability, goal, target) even for a run that
    # crashes or hangs before ever reaching a terminal "discovery_result" event.
    logger.log("system", "run_start", kind="discovery", capability_id=spec.capability_id, goal=spec.goal, target=args.target)
    safety_policy = runtime.build_safety_policy(target["base_url"], target["allowlist"])

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

        loop = DiscoveryLoop(surface, GeminiClient(), evidence_logger=logger, max_steps=args.max_steps, timeout_seconds=args.timeout, session_manager=session)
        result = loop.run(goal=spec.goal, parameters=params, start_path=spec.start_path)
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

    artifact = build_artifact(
        result, params,
        capability_id=spec.capability_id, version=spec.version, name=spec.name, description=spec.description,
        target=spec.target, preconditions=spec.preconditions,
        input_schema=spec.input_schema, output_schema=spec.output_schema,
        success_checkpoint=spec.success_checkpoint, error_handling=spec.error_handling,
        safety=spec.safety, success_output_defaults=spec.success_output_defaults,
        discovered_by=GeminiClient().model, discovery_run_id=evidence_dir.name,
    )
    saved_path = save_artifact(artifact)
    (evidence_dir / "artifact.json").write_text(artifact.model_dump_json(indent=2))
    print(f"saved artifact: {saved_path}")
    print(f"evidence: {evidence_dir}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    load_dotenv()
    artifact = load_artifact_by_id(args.capability)
    params = parse_params(args.param, input_schema=artifact.input_schema)
    # Computed here, not left to run_replay's own default, so the session id (== evidence_dir
    # name) is known up front -- the operator console URL can then be printed *before* the run
    # starts, matching discover's behavior, rather than only after it finishes.
    evidence_dir = Path(args.evidence_dir) if args.evidence_dir else EVIDENCE_ROOT / runtime.run_id("replay_run")

    if not args.no_operator_console:
        print(f"operator console (visit if this run pauses): http://127.0.0.1:{args.operator_port}/operator/{evidence_dir.name}")

    result, evidence_dir = runtime.run_replay(
        args.capability, params,
        target=args.target, base_url=args.base_url, username=args.username, password=args.password, allowlist=args.allowlist,
        headed=args.headed, slow_mo=args.slow_mo, evidence_dir=evidence_dir,
        operator_port=args.operator_port, enable_operator_console=not args.no_operator_console,
    )

    print(f"status: {result.status.value}")
    if result.outputs is not None:
        print("outputs:", json.dumps(result.outputs, indent=2))
    if result.business_outcome:
        print(f"business_outcome: {result.business_outcome}")
    if result.error:
        print(f"error: {result.error.message}" + (f" (step {result.error.step_id})" if result.error.step_id else ""))
    print(f"evidence: {evidence_dir}")
    return 1 if result.status == ReplayStatus.HARD_FAILURE else 0


def main() -> int:
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
    replay_p.set_defaults(func=cmd_replay)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
