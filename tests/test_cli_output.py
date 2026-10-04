"""The CLI prints what a person reads. It regressed once during the QA pass (the status line vanished)."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime

import cli
import runtime
from replay.result import ReplayResult, ReplayStatus


def args(**kw):
    base = dict(capability="mockbank.member_balance_lookup", param=["member_id=10001"], evidence_dir=None, no_operator_console=True, operator_port=8010,
                resume=None, target="mockbank", base_url=None, username=None, password=None, allowlist=None, headed=False, slow_mo=0,
                dry_run=False, timeout=5, idempotency_key=None, requested_by="t", repair_llm=False)
    return argparse.Namespace(**{**base, **kw})


def test_replay_prints_status_run_and_error(monkeypatch, capsys, tmp_path):
    from replay.result import ReplayError

    now = datetime.now(UTC)
    monkeypatch.setattr(runtime, "run_replay", lambda *a, **k: (ReplayResult(
        status=ReplayStatus.HARD_FAILURE, capability_id="x.y", run_id="run_1", error=ReplayError(message="boom", step_id="s2", code="timeout"),
        started_at=now, finished_at=now), tmp_path))

    code = cli.cmd_replay(args())
    out = capsys.readouterr().out

    assert code == 1
    assert "status: hard_failure" in out and "run: run_1" in out and "error: boom (step s2) [timeout]" in out


def test_unknown_capability_and_bad_resume_are_friendly_errors(capsys):
    assert cli.cmd_replay(args(capability="clinic.nope")) == 1
    assert "unknown capability 'clinic.nope'" in capsys.readouterr().out
    assert cli.cmd_replay(args(resume="run_missing")) == 1
    assert "error:" in capsys.readouterr().out
