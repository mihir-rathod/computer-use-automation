"""Edge cases in discovery that only show up with real, unfamiliar systems. Each test is a failure that was found, or that follows directly from one: the
example value pulled out of the wrong place, a success text that belongs to one record, a field typed twice, a system that goes away mid-run."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.genai import types
from pydantic import ValidationError

from agent.loop import DiscoveryLoop, RecordedAction
from agent.recorder import _parameterize
from artifacts_lib.schema import ActionType, Locator, LocatorStrategy, Target
from surface.base import Action, ActionResult, ObservedElement, ObservedState
from discover.contract import DiscoveryContract
from discover.derive import derive_success_text
from discover.prune import prune


# ---- the example value is replaced only where it stands alone -------------------------------------------------------------

@pytest.mark.parametrize("text, params, expected", [
    ("2026-03-04", {"n": "20"}, "2026-03-04"),                                          # not pulled out of a date
    ("A-200021", {"a": "A-20002"}, "A-200021"),                                         # not out of a longer number
    ("A-20002 then A-200021", {"a": "A-20002"}, "{{a}} then A-200021"),
    ("10 and 100", {"p": "10", "q": "100"}, "{{p}} and {{q}}"),                          # the longer value is taken first
    ("member100987@example.com", {"id": "100987", "email": "member100987@example.com"}, "{{email}}"),
    ("/legacy/appointments/A-20002/cancel", {"a": "A-20002"}, "/legacy/appointments/{{a}}/cancel"),
    ("15.00", {"amt": "15.00", "other": "5.00"}, "{{amt}}"),
    ("115.00", {"amt": "15.00"}, "115.00"),
])
def test_parameterizing_replaces_an_example_only_where_it_stands_alone(text, params, expected):
    assert _parameterize(text, params) == expected


def contract(**over):
    base = {"task_name": "look_up", "name": "Look up", "description": "Looks a thing up", "target": "clinic", "goal": "Open the thing and read it, then finish.",
            "inputs": [{"name": "appt", "example": "A-20002"}, {"name": "mrn", "example": "LK-100001"}], "outputs": [{"name": "xyz"}], "effect": "read_only"}
    return DiscoveryContract.model_validate({**base, **over})


def test_two_inputs_with_the_same_example_are_refused():
    with pytest.raises(ValidationError, match="same example"):
        contract(inputs=[{"name": "appt", "example": "A-20002"}, {"name": "mrn", "example": "a-20002"}])


def test_a_one_character_example_is_refused():
    with pytest.raises(ValidationError):
        contract(inputs=[{"name": "appt", "example": "5"}])


# ---- fakes for the loop and the pruning --------------------------------------------------------------------------------------

def el(ref, role, name=None, **kw):
    return ObservedElement(ref=ref, role=role, name=name, **kw)


def state(path, *elements):
    return ObservedState(url=f"http://x.test{path}", title="t", elements=list(elements))


def target(name):
    return Target(semantic_description=name, locators=[Locator(strategy=LocatorStrategy.ROLE, value=f"textbox[name='{name}']")])


def rec(kind, before, after, ref=None, ok=True, output=None, value=None, text=None, url=None, tgt=None):
    params = {"text": text} if text is not None else ({"url": url} if url else {})
    result = ActionResult(success=ok, resolved_target=tgt, extracted_value=value)
    return RecordedAction(action=Action(kind=kind, ref=ref, params=params, actor="agent") if kind != ActionType.NAVIGATE else Action(kind=kind, params=params, actor="agent"),
                          result=result, observed_before=before, observed_after=after, output_name=output)


def test_typing_into_the_start_page_is_not_going_back_to_it_and_a_corrected_typo_is_typed_once():
    entry, detail = state("/start", el("e1", "textbox", "No.")), state("/detail", el("c1", "cell", "Provider"), el("c2", "cell", "Dr. Okafor"))
    box = target("No.")
    transcript = [
        rec(ActionType.NAVIGATE, state("/menu"), entry, url="/start"),
        rec(ActionType.TYPE, entry, entry, ref="e1", text="A-2000", tgt=box),      # a typo
        rec(ActionType.TYPE, entry, entry, ref="e1", text="A-20002", tgt=box),     # corrected
        rec(ActionType.CLICK, entry, detail, ref="b1"),
        rec(ActionType.EXTRACT, detail, detail, ref="c2", output="provider", value="Dr. Okafor"),
        rec(ActionType.EXTRACT, detail, detail, ref="c2", output="provider", value="Dr. Okafor"),   # read twice
        rec(ActionType.EXTRACT, detail, detail, ref="c1", output="extra", value="Provider"),        # not something the task returns
    ]
    kept, dropped = prune(transcript, {"provider"})
    assert [(r.action.kind.value, r.action.params.get("text")) for r in kept] == [("navigate", None), ("type", "A-20002"), ("click", None), ("extract", None)]
    assert dropped == 3


def test_a_failed_action_and_a_trip_back_to_the_start_are_dropped():
    start, other, detail = state("/start", el("l", "link", "Go")), state("/other"), state("/detail")
    transcript = [
        rec(ActionType.NAVIGATE, state("/menu"), start, url="/start"),
        rec(ActionType.CLICK, start, other, ref="l"),                       # a detour...
        rec(ActionType.CLICK, other, other, ref="zzz", ok=False),           # ...a failed attempt...
        rec(ActionType.CLICK, other, start, ref="back"),                    # ...and back to where it started
        rec(ActionType.CLICK, start, detail, ref="l"),                      # the way that worked
    ]
    kept, dropped = prune(transcript)
    assert [r.action.ref for r in kept] == [None, "l"] and kept[1].observed_after.url.endswith("/detail") and dropped == 3


# ---- success text: never something that belongs to the record that was read ------------------------------------------------------

def test_the_success_text_is_never_a_value_that_was_read_or_typed():
    entry = state("/start", el("h0", "heading", "Find a person"))
    answer = state("/detail", el("h1", "heading", "Brennan, Avery"), el("c0", "cell", "Name"), el("c1", "cell", "Brennan, Avery"), el("c2", "cell", "Okafor"))
    reads = [rec(ActionType.EXTRACT, answer, answer, ref="c1", output="name", value="Brennan, Avery"), rec(ActionType.EXTRACT, answer, answer, ref="c2", output="provider", value="Okafor")]
    transcript = [rec(ActionType.NAVIGATE, state("/menu"), entry, url="/start"), rec(ActionType.CLICK, entry, answer, ref="go"), *reads]

    class Page:
        def check_signal(self, signal):
            return True

    # the heading and the cell before "Okafor" are both a read value; the label "Name" is what is left
    assert derive_success_text(transcript, Page(), ["A-20002"]) == "Name"


# ---- the loop ---------------------------------------------------------------------------------------------------------------

class Script:
    def __init__(self, steps):
        self.steps, self.i, self.prompts = steps, 0, []

    def generate(self, contents, tools=None, system_instruction=None):
        self.prompts.append(contents[-1].parts[0].text)
        name, args = self.steps[min(self.i, len(self.steps) - 1)]
        self.i += 1
        part = types.Part.from_function_call(name=name, args=args)
        return SimpleNamespace(candidates=[SimpleNamespace(content=types.Content(role="model", parts=[part]))])


class FakeSurface:
    def __init__(self, pages, fail_with=None):
        self.pages, self.fail_with, self.at = pages, fail_with, 0

    def perceive(self, actor="system"):
        return self.pages[self.at % len(self.pages)]

    def act(self, action):
        if self.fail_with:
            return ActionResult(success=False, error=self.fail_with)
        self.at += 1
        return ActionResult(success=True)


def test_a_system_that_goes_away_ends_the_session_at_once_instead_of_guessing_at_an_error_page():
    surface = FakeSurface([state("/menu", el("a", "link", "Go"))], fail_with="Page.goto: net::ERR_CONNECTION_REFUSED at http://localhost:8100/legacy/menu")
    model = Script([("click", {"ref": "a"})] * 30)
    result = DiscoveryLoop(surface, model, max_steps=25).run("do it", {})
    assert result.stop_reason == "error" and "could not be reached" in result.reasoning and model.i == 1


def test_a_model_going_round_in_circles_is_told_so():
    pages = [state("/menu", el("a", "link", "A")), state("/a", el("b", "link", "Menu"))]
    model = Script([("click", {"ref": "a"}), ("click", {"ref": "b"})] * 6)
    DiscoveryLoop(FakeSurface(pages), model, max_steps=8).run("do it", {})
    nudged = [p for p in model.prompts if "have been on this page" in p]
    assert nudged and "3 times" in nudged[0] and not any("have been on this page" in p for p in model.prompts[:4])


# ---- found while benchmarking: a link whose name has an apostrophe, and reads that only work for one record ------------------------------

def test_a_role_locator_can_name_something_with_an_apostrophe():
    from surface.locator_resolver import _ROLE_VALUE_RE
    m = _ROLE_VALUE_RE.match("link[name='Today's schedule']")
    assert m.group("role") == "link" and m.group("name") == "Today's schedule"
    assert _ROLE_VALUE_RE.match("button[name='Show']").group("name") == "Show" and _ROLE_VALUE_RE.match("textbox").group("name") is None


def test_a_read_that_finds_its_cell_by_the_value_it_read_is_caught():
    from discover.service import _reads_found_by_their_value
    box = target("x")
    by_value = Target(semantic_description="cell Brennan", locators=[Locator(strategy=LocatorStrategy.ROLE, value="cell[name='Brennan, Avery']")])
    by_label = Target(semantic_description="value beside the 'Patient' label", locators=[Locator(strategy=LocatorStrategy.XPATH, value='(//td[.="Patient"]/following-sibling::td[1])[1]')])
    page = state("/detail", el("c1", "cell", "Brennan, Avery"))

    def read(tgt):
        r = rec(ActionType.EXTRACT, page, page, ref="c1", output="patient", value="Brennan, Avery")
        r.result.resolved_target = tgt
        return r
    assert box and "only work for this one record" in _reads_found_by_their_value([read(by_value)])
    assert _reads_found_by_their_value([read(by_label)]) is None
