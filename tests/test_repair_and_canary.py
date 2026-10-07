"""Drift, repair proposals and canaries, against the clinic's UI drift modes and the checked-in clinic artifacts.

Nothing here lets the platform fix an artifact on its own: a failed replay yields a *proposal*, a person on the
policy roster approves it, and only then does a new artifact version exist. Each test works on a copy of artifacts/."""
from __future__ import annotations

import shutil
from pathlib import Path

import httpx
import pytest

from repair import canary
import runtime
from artifacts_lib import storage
from artifacts_lib.diff import diff_artifacts
from repair.apply import approve_repair, reject_repair
from repair.propose import RepairProposal
from replay.result import ReplayStatus
from runs.store import ApprovalError
from safety.config import PolicyConfig
from tests.clinic_support import copy_artifacts, effects, reset

REAL_ARTIFACTS = Path(__file__).resolve().parent.parent / "artifacts"
LOOKUP = {"mrn": "LK-100002"}


@pytest.fixture
def arts(tmp_path) -> Path:
    return copy_artifacts(tmp_path)


@pytest.fixture
def world(clinic_base_url):
    fixtures = reset(clinic_base_url)
    yield clinic_base_url, fixtures
    httpx.delete(f"{clinic_base_url}/_test/drift", timeout=5)


def drift(base: str, level: int, seed: str = "r1") -> None:
    httpx.post(f"{base}/_test/drift", json={"level": level, "seed": seed}, timeout=5).raise_for_status()


def replay(world, arts, tmp_path, cap="clinic.patient_lookup", params=None, **kw):
    return runtime.run_replay(cap, params or LOOKUP, target="clinic", base_url=world[0], enable_operator_console=False,
                              artifacts_dir=arts, evidence_dir=tmp_path / f"ev{len(list(tmp_path.glob('ev*')))}", **kw)[0]


def proposal_of(result) -> RepairProposal:
    row = runtime.default_store().get_repair(result.repair_proposal_id)
    return RepairProposal.model_validate_json(row["proposal_json"])


# ---- drift is detected, never papered over ------------------------------------------------------

def test_without_drift_the_artifact_works(world, arts, tmp_path):
    assert replay(world, arts, tmp_path).status == ReplayStatus.SUCCESS


def test_id_and_class_drift_is_absorbed_by_the_locator_fallback_chain(world, arts, tmp_path):
    """Level 1 renames ids and classes. Role+name locators come first in each chain, so nothing breaks."""
    drift(world[0], 1)
    assert replay(world, arts, tmp_path).status == ReplayStatus.SUCCESS


def heal_login(world, arts, tmp_path) -> RepairProposal:
    """Label drift breaks sign-on first ('Sign In' became 'Log in'). Sign-on is a capability like any other, so it fails
    with its own proposal; approve it so the capability under test can be reached."""
    failed = replay(world, arts, tmp_path)
    assert failed.error.code == "login_failed" and failed.repair_proposal_id, failed.error
    proposal = proposal_of(failed)
    approve_repair(runtime.default_store(), PolicyConfig.load(), proposal.id, "smooth.operator", "sign-on button was relabelled", arts)
    return proposal


def test_label_drift_breaks_sign_on_first_and_says_so(world, arts, tmp_path):
    drift(world[0], 2)
    result = replay(world, arts, tmp_path)

    assert result.status == ReplayStatus.HARD_FAILURE and result.error.code == "login_failed"
    p = proposal_of(result)
    assert p.capability_id == "clinic.login" and p.confident
    assert p.old_target.locators[0].value == "button[name='Sign In']"
    assert p.new_target.locators[0].value == "button[name='Log in']"


def test_label_drift_fails_loudly_with_a_repair_proposal(world, arts, tmp_path):
    drift(world[0], 2)
    heal_login(world, arts, tmp_path)
    result = replay(world, arts, tmp_path)

    assert result.status == ReplayStatus.HARD_FAILURE and result.error.code == "locator_unresolved"
    assert result.repair_proposal_id
    p = proposal_of(result)
    assert p.capability_id == "clinic.patient_lookup" and p.confident and p.method == "heuristic" and p.step_id == result.error.step_id
    assert p.old_target.locators[0].value == "button[name='Search']"
    assert p.new_target.locators[0].value == "button[name='Find patient']"  # the new label, found on the live page
    assert p.new_target.locators[-1].value == "button[name='Search']"        # old locators kept behind it as fallbacks
    assert p.screenshot and Path(p.screenshot).exists()
    assert runtime.default_store().get_repair(p.id)["status"] == "pending"


def test_a_proposal_changes_nothing_until_it_is_approved(world, arts, tmp_path):
    drift(world[0], 2)
    first = replay(world, arts, tmp_path)
    again = replay(world, arts, tmp_path)
    assert first.status == again.status == ReplayStatus.HARD_FAILURE
    assert storage.list_versions("clinic.login", arts) == ["1.0.0"]
    assert storage.list_versions("clinic.patient_lookup", arts) == ["1.0.0"]


# ---- approval ------------------------------------------------------------------------------------

def test_approved_repair_makes_a_new_version_that_replays_and_rolls_back(world, arts, tmp_path):
    drift(world[0], 2)
    policy, store = PolicyConfig.load(), runtime.default_store()
    heal_login(world, arts, tmp_path)
    failed = replay(world, arts, tmp_path)

    done = approve_repair(store, policy, failed.repair_proposal_id, "smooth.operator", "button was renamed in the vendor release", arts)

    assert done["to"] == "1.0.1" and done["promoted"]
    repaired = storage.load_artifact_by_id("clinic.patient_lookup", arts)
    assert repaired.version == "1.0.1" and repaired.provenance.parent_version == "1.0.0"
    assert repaired.provenance.approved_by == "smooth.operator" and repaired.provenance.repair_proposal_id == failed.repair_proposal_id
    assert "renamed" in repaired.provenance.change_note
    changed = diff_artifacts(storage.load_artifact_by_id("clinic.patient_lookup", arts, "1.0.0"), repaired)
    assert [c.step for c in changed.steps] == [failed.error.step_id] and not changed.schema and not changed.safety  # one step, nothing else

    # the repaired version only fixes what was renamed: the next breakage is a new, separate proposal
    result = replay(world, arts, tmp_path)
    assert result.status == ReplayStatus.HARD_FAILURE or result.status == ReplayStatus.SUCCESS
    assert result.error is None or result.error.step_id != failed.error.step_id

    assert storage.rollback("clinic.patient_lookup", arts, by="smooth.operator", reason="back out") == "1.0.0"
    assert replay(world, arts, tmp_path).error.step_id == failed.error.step_id  # the original breakage is back
    assert runtime.default_store().get_repair(failed.repair_proposal_id)["status"] == "approved"


def test_repair_approval_checks_roster_reason_and_state(world, arts, tmp_path):
    drift(world[0], 2)
    policy, store = PolicyConfig.load(), runtime.default_store()
    failed = replay(world, arts, tmp_path)  # sign-on's proposal is as good as any for exercising approval rules
    pid = failed.repair_proposal_id

    with pytest.raises(ApprovalError, match="not on the approver roster"):
        approve_repair(store, policy, pid, "mallory", "trust me", arts)
    with pytest.raises(ApprovalError, match="needs a reason"):
        approve_repair(store, policy, pid, "smooth.operator", " ", arts)
    reject_repair(store, pid, "smooth.operator", "wrong element")
    with pytest.raises(ApprovalError, match="already rejected"):
        approve_repair(store, policy, pid, "smooth.operator", "changed my mind", arts)
    assert storage.list_versions("clinic.login", arts) == ["1.0.0"]


def test_a_stale_proposal_is_refused_when_the_artifact_has_moved_on(world, arts, tmp_path):
    drift(world[0], 2)
    policy, store = PolicyConfig.load(), runtime.default_store()
    first = replay(world, arts, tmp_path)
    second = replay(world, arts, tmp_path)  # a second proposal against the same version
    approve_repair(store, policy, first.repair_proposal_id, "smooth.operator", "ok", arts)
    with pytest.raises(ApprovalError, match="re-run to get a fresh proposal"):
        approve_repair(store, policy, second.repair_proposal_id, "smooth.operator", "ok", arts)


def test_a_repair_that_touches_an_irreversible_step_needs_a_supervisor(world, arts, tmp_path):
    """Label drift breaks the refund flow step by step. Repairs of the ordinary steps need an operator; the repair of
    the Confirm step, which commits money, needs a supervisor."""
    base, fixtures = world
    drift(base, 2)
    policy, store = PolicyConfig.load(), runtime.default_store()
    params = {"invoice": fixtures["standard"]["refundable_invoice"], "amount": "20.00", "reason": "duplicate_payment"}
    seen_tiers = []
    for _ in range(12):
        first = runtime.run_replay("clinic.issue_refund", params, target="clinic", base_url=base, enable_operator_console=False,
                                   artifacts_dir=arts, requested_by="alex", evidence_dir=tmp_path / f"r{_}")[0]
        if first.status != ReplayStatus.PENDING_APPROVAL:
            break
        from runs.approvals import decide_run
        decide_run(store, policy, first.run_id, "approved", "suzie.visor", "refund checked")
        res = runtime.run_replay("clinic.issue_refund", {}, target=None, base_url=base, enable_operator_console=False,
                                 artifacts_dir=arts, resume_run_id=first.run_id, evidence_dir=tmp_path / f"r{_}b")[0]
        if res.status != ReplayStatus.HARD_FAILURE:
            break
        assert res.error.code in ("locator_unresolved", "login_failed") and res.repair_proposal_id
        p = proposal_of(res)
        assert p.confident, p.reason
        if p.touches_irreversible_step:
            with pytest.raises(ApprovalError, match="needs a supervisor"):
                approve_repair(store, policy, p.id, "smooth.operator", "renamed", arts)
            seen_tiers.append("supervisor")
            approve_repair(store, policy, p.id, "suzie.visor", "Confirm button renamed to 'Submit transaction'", arts)
        else:
            seen_tiers.append("operator")
            approve_repair(store, policy, p.id, "smooth.operator", "label changed in the vendor release", arts)
    assert "supervisor" in seen_tiers and "operator" in seen_tiers
    assert len(effects(base)) <= 1  # a failure at the commit step never posted twice


def test_no_confident_match_is_reported_as_such_and_cannot_be_approved(world, arts, tmp_path, page):
    from artifacts_lib.schema import Locator, LocatorStrategy, Target
    from repair.propose import propose_repair
    from surface.web import WebSurface
    from tests.clinic_support import sign_in

    art = storage.load_artifact_by_id("clinic.patient_lookup", arts)
    step = next(s for s in art.steps if s.step_id == "s3")
    ghost = Target(semantic_description="Nonexistent export button", locators=[Locator(strategy=LocatorStrategy.ROLE, value="button[name='Export to PDF']")],
                   hints={"role": "button", "name": "Export to PDF", "ordinal": 7, "same_role_count": 9, "before": ["Zebra"], "after": ["Yak"]})
    art = art.model_copy(update={"steps": [s.model_copy(update={"target": ghost}) if s.step_id == "s3" else s for s in art.steps]})

    sign_in(page, world[0])
    page.goto(f"{world[0]}/legacy/patients")
    proposal = propose_repair(art, "s3", WebSurface(page, base_url=world[0]), "rep_test")

    assert proposal is not None and not proposal.confident and proposal.new_target is None
    store = runtime.default_store()
    store.save_repair(proposal)
    with pytest.raises(ApprovalError, match="no confident match"):
        approve_repair(store, PolicyConfig.load(), "rep_test", "smooth.operator", "ok", arts)


def test_llm_picker_can_only_choose_among_real_candidates(world, arts, tmp_path):
    from types import SimpleNamespace

    from google.genai import types

    from repair.llm import make_llm_picker
    from repair.propose import Candidate

    def client_returning(ref):
        part = types.Part.from_function_call(name="choose", args={"ref": ref})
        return SimpleNamespace(generate=lambda contents, tools=None, **kw: SimpleNamespace(candidates=[SimpleNamespace(content=types.Content(role="model", parts=[part]))]))

    art = storage.load_artifact_by_id("clinic.patient_lookup", arts)
    step = next(s for s in art.steps if s.step_id == "s3")
    cands = [Candidate(ref="e1", role="button", name="A", score=0.3), Candidate(ref="e2", role="button", name="B", score=0.2)]
    assert make_llm_picker(client_returning("e2"))(step, cands) == "e2"
    assert make_llm_picker(client_returning("none"))(step, cands) is None


# ---- canaries ----------------------------------------------------------------------------------

def test_canary_passes_on_a_healthy_target_and_is_recorded(world, arts, tmp_path):
    outcomes = canary.run_all(target="clinic", base_url=world[0], artifacts_dir=arts)

    assert [o.capability_id for o in outcomes] == ["clinic.patient_lookup"] and outcomes[0].ok
    [row] = runtime.default_store().canary_history("clinic.patient_lookup")
    assert row["ok"] == 1 and row["version"] == "1.0.0"


def test_canary_catches_drift_before_real_traffic_and_attaches_the_repair_proposal(world, arts, tmp_path):
    drift(world[0], 2)
    [outcome] = canary.run_all(target="clinic", base_url=world[0], artifacts_dir=arts)

    assert not outcome.ok and outcome.repair_proposal_id
    assert proposal_of(SimpleResult(outcome.repair_proposal_id)).confident
    [row] = runtime.default_store().canary_history()
    assert row["ok"] == 0 and row["repair_id"] == outcome.repair_proposal_id


def test_canary_fails_when_the_answer_changes_even_though_the_page_works(world, arts, tmp_path):
    art = storage.load_artifact_by_id("clinic.patient_lookup", arts)
    wrong = art.canary.model_copy(update={"expect": {"status": "found", "patient_name": "Someone, Else"}})
    outcome = canary.run_canary(art.model_copy(update={"canary": wrong}), target="clinic", base_url=world[0], artifacts_dir=arts)
    assert not outcome.ok and "patient_name" in outcome.detail


def test_only_read_only_capabilities_can_have_a_canary(world, arts):
    from artifacts_lib.lint import has_errors, lint_artifact
    from artifacts_lib.schema import CanarySpec

    refund = storage.load_artifact_by_id("clinic.issue_refund", arts).model_copy(update={"canary": CanarySpec(params={}, expect={})})
    assert has_errors(lint_artifact(refund))
    assert "clinic.issue_refund" not in [a.capability_id for a in canary.canary_capabilities(arts)]


class SimpleResult:
    def __init__(self, repair_proposal_id):
        self.repair_proposal_id = repair_proposal_id


def test_repairing_a_step_also_repairs_its_checkpoint_on_the_same_element(world, arts, tmp_path):
    """Found by the drift report: after a form field was repaired, its checkpoint ('the field holds what was typed')
    still pointed at the old broken locators, so the failure just moved from the action to the checkpoint."""
    drift(world[0], 3)
    failed = replay(world, arts, tmp_path)
    p = proposal_of(failed)
    assert p.capability_id == "clinic.login" and p.step_id == "s2"
    approve_repair(runtime.default_store(), PolicyConfig.load(), p.id, "smooth.operator", "username field renamed", arts)

    repaired = storage.load_artifact_by_id("clinic.login", arts)
    s2 = next(s for s in repaired.steps if s.step_id == "s2")
    assert s2.checkpoint.target.locators == s2.target.locators
    assert any(l.value.startswith("[name='f") for l in s2.target.locators)  # the renamed field's new HTML name
    assert any(l.value == "[name='username']" for l in s2.target.locators)  # the old locator stays behind as a fallback


def test_a_lost_session_is_reported_as_one_and_does_not_create_a_repair_proposal(world, arts, tmp_path):
    """Seen in the console: a run sent back to the sign-in page mid-way produced a 'No clear match' repair proposal against the sign-in page."""
    import json

    path = arts / "clinic.patient_lookup" / "1.0.0.json"
    art = json.loads(path.read_text())
    art["error_handling"]["recoverable"] = []                   # no rule to recover from a timeout, so the failure is what a lost session really looks like
    path.write_text(json.dumps(art))
    httpx.post(f"{world[0]}/_test/chaos", json={"kind": "expire_session", "method": "GET", "path_glob": "/legacy/patients", "remaining": 1}, timeout=5)

    result = replay(world, arts, tmp_path)

    assert result.status == ReplayStatus.HARD_FAILURE and result.error.code == "session_lost"
    assert "sign-in page" in result.error.message and "Nothing was changed" in result.error.message
    assert result.repair_proposal_id is None and runtime.default_store().list_repairs() == []
