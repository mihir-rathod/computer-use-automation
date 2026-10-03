"""Schema validation tests for the capability artifact model.

Covers the two things worth testing early: the hand-written
fixture parses and round-trips through JSON, and the validators that make "typed
inputs/outputs" and "reviewable" real constraints rather than aspirational ones.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from artifacts_lib import Artifact
from artifacts_lib.storage import load_artifact, save_artifact

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "mockbank.member_balance_lookup.json"


@pytest.fixture
def valid_artifact_dict() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def test_fixture_loads_via_storage():
    artifact = load_artifact(FIXTURE_PATH)
    assert artifact.capability_id == "mockbank.member_balance_lookup"
    assert len(artifact.steps) == 6
    assert artifact.safety.risk_level == "read_only"


def test_round_trip_save_and_load(tmp_path, valid_artifact_dict):
    artifact = Artifact.model_validate(valid_artifact_dict)
    path = save_artifact(artifact, directory=tmp_path)
    reloaded = load_artifact(path)
    assert reloaded == artifact


def test_duplicate_step_id_rejected(valid_artifact_dict):
    valid_artifact_dict["steps"][1]["step_id"] = valid_artifact_dict["steps"][0]["step_id"]
    with pytest.raises(ValidationError, match="duplicate step_id"):
        Artifact.model_validate(valid_artifact_dict)


def test_click_without_target_rejected(valid_artifact_dict):
    click_step = next(s for s in valid_artifact_dict["steps"] if s["action"] == "click")
    click_step["target"] = None
    with pytest.raises(ValidationError, match="requires a target"):
        Artifact.model_validate(valid_artifact_dict)


def test_extract_without_output_binding_rejected(valid_artifact_dict):
    extract_step = next(s for s in valid_artifact_dict["steps"] if s["action"] == "extract")
    extract_step["output_binding"] = None
    with pytest.raises(ValidationError, match="requires output_binding"):
        Artifact.model_validate(valid_artifact_dict)


def test_unknown_output_binding_rejected(valid_artifact_dict):
    extract_step = next(s for s in valid_artifact_dict["steps"] if s["action"] == "extract")
    extract_step["output_binding"] = "not_a_declared_output_field"
    with pytest.raises(ValidationError, match="not declared in output_schema"):
        Artifact.model_validate(valid_artifact_dict)


@pytest.mark.parametrize("bad_id", ["MockBank.member_balance_lookup", "mockbank", "mockbank-member-lookup"])
def test_bad_capability_id_format_rejected(valid_artifact_dict, bad_id):
    valid_artifact_dict["capability_id"] = bad_id
    with pytest.raises(ValidationError, match="capability_id"):
        Artifact.model_validate(valid_artifact_dict)


@pytest.mark.parametrize("bad_version", ["1.0", "v1.0.0", "1.0.0-beta"])
def test_bad_version_format_rejected(valid_artifact_dict, bad_version):
    valid_artifact_dict["version"] = bad_version
    with pytest.raises(ValidationError, match="version"):
        Artifact.model_validate(valid_artifact_dict)


def test_element_signal_without_target_rejected(valid_artifact_dict):
    valid_artifact_dict["success_checkpoint"]["target"] = None
    with pytest.raises(ValidationError, match="requires a target"):
        Artifact.model_validate(valid_artifact_dict)


def test_non_element_signal_with_target_rejected(valid_artifact_dict):
    rule = valid_artifact_dict["error_handling"]["business_outcomes"][0]
    rule["signal"]["target"] = copy.deepcopy(valid_artifact_dict["success_checkpoint"]["target"])
    with pytest.raises(ValidationError, match="does not use a target"):
        Artifact.model_validate(valid_artifact_dict)


def test_retry_with_zero_max_attempts_rejected(valid_artifact_dict):
    retry_rule = next(r for r in valid_artifact_dict["error_handling"]["recoverable"] if r["action"] == "retry")
    retry_rule["max_attempts"] = 0
    with pytest.raises(ValidationError, match="max_attempts"):
        Artifact.model_validate(valid_artifact_dict)


def test_required_output_field_not_in_properties_rejected(valid_artifact_dict):
    valid_artifact_dict["output_schema"]["required"] = ["totally_undeclared_field"]
    with pytest.raises(ValidationError, match="not present in properties"):
        Artifact.model_validate(valid_artifact_dict)


def test_empty_steps_rejected(valid_artifact_dict):
    valid_artifact_dict["steps"] = []
    with pytest.raises(ValidationError, match="at least one step"):
        Artifact.model_validate(valid_artifact_dict)
