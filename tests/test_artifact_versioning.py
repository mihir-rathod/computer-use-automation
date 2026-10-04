from __future__ import annotations

import json
from pathlib import Path

import pytest

from artifacts_lib import Artifact
from artifacts_lib.diff import diff_artifacts
from artifacts_lib.lint import Finding, has_errors, lint_artifact
from artifacts_lib.storage import (
    UnknownVersion, VersionExists, current_version, history, list_artifacts, list_versions, load_artifact_by_id,
    migrate_flat, next_version, rollback, save_artifact, set_current,
)

FIXTURE = Path(__file__).parent / "fixtures" / "mockbank.member_balance_lookup.json"
REAL_OPEN = Path(__file__).resolve().parent.parent / "artifacts" / "mockbank.open_subaccount"


def base() -> Artifact:
    return Artifact.model_validate(json.loads(FIXTURE.read_text()))


def bumped(version: str, **changes) -> Artifact:
    data = json.loads(FIXTURE.read_text())
    data["version"] = version
    data.update(changes)
    return Artifact.model_validate(data)


def codes(findings: list[Finding]) -> set[str]:
    return {f.code for f in findings}


# ---- storage ---------------------------------------------------------------------------

def test_first_version_is_current_and_later_ones_are_candidates(tmp_path):
    save_artifact(base(), tmp_path)
    assert current_version("mockbank.member_balance_lookup", tmp_path) == "1.0.0"
    save_artifact(bumped("1.0.1"), tmp_path)
    assert list_versions("mockbank.member_balance_lookup", tmp_path) == ["1.0.0", "1.0.1"]
    assert current_version("mockbank.member_balance_lookup", tmp_path) == "1.0.0"  # not silently replaced
    assert load_artifact_by_id("mockbank.member_balance_lookup", tmp_path).version == "1.0.0"
    assert load_artifact_by_id("mockbank.member_balance_lookup", tmp_path, version="1.0.1").version == "1.0.1"


def test_saving_never_overwrites(tmp_path):
    save_artifact(base(), tmp_path)
    with pytest.raises(VersionExists):
        save_artifact(base(), tmp_path)


def test_promote_and_rollback_keep_an_audit_history(tmp_path):
    cid = "mockbank.member_balance_lookup"
    save_artifact(base(), tmp_path)
    save_artifact(bumped("1.1.0"), tmp_path)
    set_current(cid, "1.1.0", tmp_path, by="mihir", reason="canary passed")
    assert current_version(cid, tmp_path) == "1.1.0"
    assert rollback(cid, tmp_path, by="mihir", reason="regression") == "1.0.0"
    assert current_version(cid, tmp_path) == "1.0.0"
    assert [(e["action"], e["version"], e["by"]) for e in history(cid, tmp_path)] == [
        ("initial", "1.0.0", "system"), ("promote", "1.1.0", "mihir"), ("rollback", "1.0.0", "mihir")]
    with pytest.raises(UnknownVersion):
        set_current(cid, "9.9.9", tmp_path)


def test_next_version_bumps(tmp_path):
    cid = "mockbank.member_balance_lookup"
    assert next_version(cid, tmp_path) == "1.0.0"
    save_artifact(base(), tmp_path)
    assert next_version(cid, tmp_path) == "1.0.1"
    assert next_version(cid, tmp_path, "minor") == "1.1.0"
    assert next_version(cid, tmp_path, "major") == "2.0.0"


def test_flat_layout_is_read_and_can_be_migrated(tmp_path):
    (tmp_path / "mockbank.member_balance_lookup.json").write_text(FIXTURE.read_text())
    assert load_artifact_by_id("mockbank.member_balance_lookup", tmp_path).version == "1.0.0"
    assert [a.capability_id for a in list_artifacts(tmp_path)] == ["mockbank.member_balance_lookup"]
    assert migrate_flat(tmp_path) == ["mockbank.member_balance_lookup"]
    assert (tmp_path / "mockbank.member_balance_lookup" / "1.0.0.json").exists()
    assert not (tmp_path / "mockbank.member_balance_lookup.json").exists()


# ---- lint ------------------------------------------------------------------------------

def test_a_clean_real_artifact_has_no_errors():
    from artifacts_lib.storage import DEFAULT_ARTIFACTS_DIR

    findings = lint_artifact(load_artifact_by_id("mockbank.open_subaccount", DEFAULT_ARTIFACTS_DIR))
    assert not has_errors(findings), [str(f) for f in findings]


def test_lint_catches_an_irreversible_step_that_is_marked_idempotent():
    data = json.loads((REAL_OPEN / "1.0.0.json").read_text())
    for step in data["steps"]:
        if step["risk_level"] == "irreversible":
            step["idempotent"] = True
    findings = lint_artifact(Artifact.model_validate(data))
    assert "irreversible-idempotent" in codes(findings) and has_errors(findings)


def test_lint_catches_unconfirmed_irreversible_and_readonly_mislabel():
    data = json.loads((REAL_OPEN / "1.0.0.json").read_text())
    data["safety"] = {"risk_level": "read_only", "requires_confirmation": False}
    assert {"irreversible-unconfirmed", "readonly-has-irreversible"} <= codes(lint_artifact(Artifact.model_validate(data)))


def test_lint_catches_undeclared_template_and_secret_literal():
    data = json.loads(FIXTURE.read_text())
    data["steps"][1]["params"] = {"text": "{{not_declared}}"}
    assert "undeclared-template-variable" in codes(lint_artifact(Artifact.model_validate(data)))

    login = json.loads((Path(__file__).parent / "fixtures" / "mockbank.login.json").read_text())
    for step in login["steps"]:
        if step["action"] == "type" and "assword" in step["target"]["semantic_description"]:
            step["params"] = {"text": "hunter2"}
    assert "secret-literal" in codes(lint_artifact(Artifact.model_validate(login)))


def test_lint_warns_but_does_not_fail_for_review_and_unused_input():
    data = json.loads(FIXTURE.read_text())
    data["input_schema"]["properties"]["spare"] = {"type": "string"}
    data["provenance"]["reviewed"] = False
    findings = lint_artifact(Artifact.model_validate(data))
    assert {"unused-input", "unreviewed"} <= codes(findings) and not has_errors(findings)


# ---- diff ------------------------------------------------------------------------------

def test_diff_of_identical_artifacts_is_empty():
    assert diff_artifacts(base(), bumped("1.0.0")).is_empty


def test_diff_reports_locator_param_and_step_changes():
    a = base()
    data = json.loads(FIXTURE.read_text())
    data["version"] = "1.0.1"
    data["steps"][2]["target"]["locators"].insert(0, {"strategy": "css", "value": "#renamed"})
    data["steps"][0]["params"]["url"] = "/somewhere-else"
    extra = json.loads(json.dumps(data["steps"][-1]))
    extra["step_id"] = "s99"
    data["steps"].append(extra)
    data["provenance"]["approved_by"] = "mihir"
    d = diff_artifacts(a, Artifact.model_validate(data))
    text = d.to_text()
    assert "1.0.0 -> 1.0.1" in text
    assert any(c.kind == "changed" and any("locators" in x for x in c.details) for c in d.steps)
    assert any(c.kind == "changed" and any("params" in x for x in c.details) for c in d.steps)
    assert any(c.kind == "added" and c.step == "s99" for c in d.steps)
    assert any("approved_by" in m for m in d.metadata)


def test_diff_reports_input_schema_changes():
    data = json.loads(FIXTURE.read_text())
    data["version"] = "2.0.0"
    data["input_schema"]["properties"]["extra"] = {"type": "string"}
    d = diff_artifacts(base(), Artifact.model_validate(data))
    assert "input added: extra" in d.schema


def test_lint_flags_a_url_checkpoint_that_names_one_record():
    from artifacts_lib.schema import Signal, SignalType

    artifact = base()
    step = artifact.steps[0].model_copy(update={"checkpoint": Signal(type=SignalType.URL_MATCHES, value="**/legacy/patients/1")})
    artifact = artifact.model_copy(update={"steps": [step, *artifact.steps[1:]]})
    assert "url-checkpoint-record-id" in codes(lint_artifact(artifact))
    ok = artifact.model_copy(update={"steps": [step.model_copy(update={"checkpoint": Signal(type=SignalType.URL_MATCHES, value="**/legacy/patients/*")}), *artifact.steps[1:]]})
    assert "url-checkpoint-record-id" not in codes(lint_artifact(ok))
