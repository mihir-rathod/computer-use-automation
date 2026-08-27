#!/usr/bin/env python
"""CLI entry point. See README.md's "Demo path" for the exact commands this implements.

    uv run python cli.py discover --capability mockbank.member_balance_lookup --param member_id=10001
    uv run python cli.py replay    --capability mockbank.member_balance_lookup --param member_id=10002

Both commands log in first (via the mockbank.login artifact, replayed like any other
capability -- login is a first-class reusable capability, not special-cased CLI logic) and
write structured evidence -- a JSONL log of every perceive/act, screenshots, and the final
artifact or result -- to /evidence/<run>/ by default.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from agent.catalog import get_spec
from agent.gemini_client import GeminiClient
from agent.loop import DiscoveryLoop
from agent.recorder import build_artifact
from artifacts_lib.storage import load_artifact_by_id, save_artifact
from escalation.operator_console import app as operator_app
from escalation.registry import register_session, unregister_session
from escalation.session_manager import SessionManager
from evidence_lib.logger import EvidenceLogger
from replay.engine import ReplayEngine
from replay.result import ReplayStatus
from safety.allowlist import DEFAULT_ALLOWLIST_PATH, AllowlistConfig, AllowlistPolicy
from safety.policy import SafetyPolicy
from surface.web import WebSurface

REPO_ROOT = Path(__file__).resolve().parent
EVIDENCE_ROOT = REPO_ROOT / "evidence"
_operator_console_started = False

# Adapting to a new target is choosing a profile here, not writing code -- each entry pairs a
# base URL with the allowlist config and login capability that go with it. `--target` selects
# one; any of --base-url/--username/--password/--allowlist still overrides its piece explicitly.
TARGET_PROFILES: dict[str, dict[str, Any]] = {
    "mockbank": {
        "base_url": os.environ.get("MOCKBANK_BASE_URL", "http://localhost:8000"),
        "username": "operator",
        "password": "bankdemo123",
        "allowlist": DEFAULT_ALLOWLIST_PATH,
        "login_capability": "mockbank.login",
        "login_path": "/login",
    },
    "meridian": {
        "base_url": "https://web-sample.interface-hiring.com",
        "username": "teller1",
        "password": "password",
        "allowlist": REPO_ROOT / "safety" / "allowlist_meridian.json",
        "login_capability": "meridian.signon",
        "login_path": "/signon",
    },
}


def resolve_target(args: argparse.Namespace) -> dict[str, Any]:
    """Merges the selected --target profile with any explicit CLI overrides -- adapting to a
    new target is picking a profile, not writing code; an override still wins per-field when
    given (e.g. --base-url against a locally proxied copy of the same target)."""
    profile = dict(TARGET_PROFILES[args.target])
    if args.base_url is not None:
        profile["base_url"] = args.base_url
    if args.username is not None:
        profile["username"] = args.username
    if args.password is not None:
        profile["password"] = args.password
    if args.allowlist is not None:
        profile["allowlist"] = Path(args.allowlist)
    return profile


def ensure_operator_console(port: int) -> None:
    """Starts the operator console once per process, in a background thread. Only the
    automation thread (the one running discover/replay, below) ever touches the live
    Playwright page -- this thread only serves HTML and enqueues human-submitted intents via
    SessionManager.request_action (see escalation/session_manager.py's module docstring)."""
    global _operator_console_started
    if _operator_console_started:
        return
    config = uvicorn.Config(operator_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    _operator_console_started = True


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


def build_safety_policy(base_url: str, allowlist_path: Path = DEFAULT_ALLOWLIST_PATH) -> SafetyPolicy:
    config = AllowlistConfig.from_json(allowlist_path)
    # allowed_route_patterns/action_types come from the checked-in policy; allowed_base_urls is
    # overridden to whatever --base-url actually is, so the policy always matches where this
    # run is really pointed rather than silently drifting from the JSON file's documented default.
    config = config.model_copy(update={"allowed_base_urls": [base_url]})
    return SafetyPolicy(AllowlistPolicy(config))


def run_login(surface: WebSurface, username: str, password: str, login_capability: str = "mockbank.login") -> None:
    login_artifact = load_artifact_by_id(login_capability)
    result = ReplayEngine(surface).run(login_artifact, {"username": username, "password": password})
    if result.status != ReplayStatus.SUCCESS:
        raise SystemExit(f"login failed: status={result.status.value} error={result.error}")


def _run_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def cmd_discover(args: argparse.Namespace) -> int:
    load_dotenv()
    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit("GEMINI_API_KEY is not set -- see README.md Setup (needed for `discover`, not `replay`).")

    target = resolve_target(args)
    spec = get_spec(args.capability, target["base_url"])
    params = parse_params(args.param)
    evidence_dir = Path(args.evidence_dir) if args.evidence_dir else EVIDENCE_ROOT / _run_id("discovery_run")
    logger = EvidenceLogger(evidence_dir)
    safety_policy = build_safety_policy(target["base_url"], target["allowlist"])

    if not args.no_operator_console:
        ensure_operator_console(args.operator_port)
    session = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page()
        page.goto(f"{target['base_url']}{target['login_path']}")

        surface = WebSurface(page, base_url=target["base_url"], screenshot_dir=evidence_dir / "screenshots", evidence_logger=logger, safety_policy=safety_policy)
        # Discovering the target's own login capability is the one case where we must NOT log
        # in first -- that would be circular (you can't sign on before discovering how to sign
        # on). Every other capability assumes an authenticated session, hence the skip is keyed
        # off exactly this one id, not a general flag.
        if args.capability != target["login_capability"]:
            run_login(surface, target["username"], target["password"], login_capability=target["login_capability"])

        if not args.no_operator_console:
            session = SessionManager(evidence_dir.name, surface, evidence_dir, evidence_logger=logger, capability_id=spec.capability_id, goal=spec.goal)
            register_session(session)
            print(f"operator console (visit if this run pauses): http://127.0.0.1:{args.operator_port}/operator/{session.session_id}")

        loop = DiscoveryLoop(surface, GeminiClient(), evidence_logger=logger, max_steps=args.max_steps, timeout_seconds=args.timeout, session_manager=session)
        result = loop.run(goal=spec.goal, parameters=params, start_path=spec.start_path)
        if session is not None:
            unregister_session(session.session_id)
        browser.close()

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
    target = resolve_target(args)
    artifact = load_artifact_by_id(args.capability)
    params = parse_params(args.param, input_schema=artifact.input_schema)
    evidence_dir = Path(args.evidence_dir) if args.evidence_dir else EVIDENCE_ROOT / _run_id("replay_run")
    logger = EvidenceLogger(evidence_dir)
    safety_policy = build_safety_policy(target["base_url"], target["allowlist"])

    if not args.no_operator_console:
        ensure_operator_console(args.operator_port)
    session = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page()
        page.goto(f"{target['base_url']}{target['login_path']}")

        surface = WebSurface(page, base_url=target["base_url"], screenshot_dir=evidence_dir / "screenshots", evidence_logger=logger, safety_policy=safety_policy)
        run_login(surface, target["username"], target["password"], login_capability=target["login_capability"])

        if not args.no_operator_console:
            session = SessionManager(evidence_dir.name, surface, evidence_dir, evidence_logger=logger, capability_id=args.capability, goal=None)
            register_session(session)
            print(f"operator console (visit if this run pauses): http://127.0.0.1:{args.operator_port}/operator/{session.session_id}")

        engine = ReplayEngine(
            surface, evidence_logger=logger,
            reauth_credentials={"username": target["username"], "password": target["password"]},
            session_manager=session,
        )
        result = engine.run(artifact, params)
        if session is not None:
            unregister_session(session.session_id)
        browser.close()

    print(f"status: {result.status.value}")
    if result.outputs is not None:
        print("outputs:", json.dumps(result.outputs, indent=2))
    if result.business_outcome:
        print(f"business_outcome: {result.business_outcome}")
    if result.error:
        print(f"error: {result.error.message}" + (f" (step {result.error.step_id})" if result.error.step_id else ""))

    (evidence_dir / "result.json").write_text(result.model_dump_json(indent=2))
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
    replay_p.add_argument("--evidence-dir", default=None)
    replay_p.set_defaults(func=cmd_replay)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
