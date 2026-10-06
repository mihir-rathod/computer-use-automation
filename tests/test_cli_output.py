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


def test_artifact_commands_handle_a_draft_that_was_discovered_but_never_promoted(monkeypatch, capsys, tmp_path):
    """A draft has versions but no current one; listing and validating every artifact must not crash on it."""
    import shutil
    from artifacts_lib import storage

    src = storage.DEFAULT_ARTIFACTS_DIR / "clinic.patient_lookup"
    shutil.copytree(src, tmp_path / "clinic.draft_one", ignore=shutil.ignore_patterns("index.json"))
    for f in (tmp_path / "clinic.draft_one").glob("*.json"):
        text = f.read_text().replace("clinic.patient_lookup", "clinic.draft_one")
        f.write_text(text)
    for keep in sorted((tmp_path / "clinic.draft_one").glob("*.json"))[1:]:
        keep.unlink()
    (tmp_path / "clinic.draft_one" / "index.json").write_text('{"current": null, "history": []}')
    for name in ("capability_ids", "list_versions", "current_version"):
        real = getattr(storage, name)
        n_args = 1 if name == "capability_ids" else 2  # the directory is the last positional argument; add it only when the caller left it out
        monkeypatch.setattr(storage, name, lambda *a, _real=real, _n=n_args, **k: _real(*a, **k) if len(a) >= _n or "directory" in k else _real(*a, directory=tmp_path, **k))
    real_load = cli.load_artifact_by_id
    monkeypatch.setattr(cli, "load_artifact_by_id", lambda cid, version=None: real_load(cid, tmp_path, version=version))

    assert cli.cmd_artifact(argparse.Namespace(artifact_command="list")) == 0
    assert "a draft: not runnable until promoted" in capsys.readouterr().out
    cli.cmd_artifact(argparse.Namespace(artifact_command="validate", all=True, capability=None, version=None))
    assert "clinic.draft_one" in capsys.readouterr().out
