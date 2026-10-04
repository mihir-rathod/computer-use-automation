"""`artifact diff`: what actually changed between two versions of a capability, in terms a reviewer can
act on (which step's locator changed, which input appeared) rather than a raw JSON diff."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Any

from artifacts_lib.schema import Artifact, Step


@dataclass
class StepChange:
    kind: str  # added | removed | changed
    step: str
    summary: str
    details: list[str] = field(default_factory=list)


@dataclass
class ArtifactDiff:
    capability_id: str
    version_a: str
    version_b: str
    metadata: list[str] = field(default_factory=list)
    schema: list[str] = field(default_factory=list)
    steps: list[StepChange] = field(default_factory=list)
    safety: list[str] = field(default_factory=list)
    error_handling: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.metadata or self.schema or self.steps or self.safety or self.error_handling)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_text(self) -> str:
        lines = [f"{self.capability_id}: {self.version_a} -> {self.version_b}"]
        if self.is_empty:
            return lines[0] + "\n  (no differences)"
        for title, items in (("metadata", self.metadata), ("input/output", self.schema), ("safety", self.safety),
                             ("error handling", self.error_handling)):
            if items:
                lines += [f"  {title}:", *[f"    {i}" for i in items]]
        if self.steps:
            lines.append("  steps:")
            mark = {"added": "+", "removed": "-", "changed": "~"}
            for change in self.steps:
                lines.append(f"    {mark[change.kind]} {change.step}  {change.summary}")
                lines += [f"        {d}" for d in change.details]
        return "\n".join(lines)


def _describe(step: Step) -> str:
    where = step.target.semantic_description if step.target else step.params.get("url", "")
    return f"{step.action.value} {where}".strip()


def _locators(step: Step) -> list[str]:
    return [f"{loc.strategy.value}:{loc.value}" for loc in (step.target.locators if step.target else [])]


def _step_details(a: Step, b: Step) -> list[str]:
    details = []
    if _locators(a) != _locators(b):
        details.append(f"locators: {_locators(a)} -> {_locators(b)}")
    if a.params != b.params:
        details.append(f"params: {a.params} -> {b.params}")
    if a.checkpoint != b.checkpoint:
        details.append("checkpoint changed")
    if a.risk_level != b.risk_level:
        details.append(f"risk: {a.risk_level.value} -> {b.risk_level.value}")
    if a.idempotent != b.idempotent:
        details.append(f"idempotent: {a.idempotent} -> {b.idempotent}")
    if a.output_binding != b.output_binding:
        details.append(f"output_binding: {a.output_binding} -> {b.output_binding}")
    return details


def _props(artifact_schema: Any) -> dict[str, Any]:
    return {k: v.get("type") for k, v in artifact_schema.properties.items()}


def _diff_props(label: str, a: Any, b: Any) -> list[str]:
    pa, pb = _props(a), _props(b)
    out = [f"{label} added: {k}" for k in sorted(set(pb) - set(pa))]
    out += [f"{label} removed: {k}" for k in sorted(set(pa) - set(pb))]
    out += [f"{label} {k}: type {pa[k]} -> {pb[k]}" for k in sorted(set(pa) & set(pb)) if pa[k] != pb[k]]
    if set(a.required) != set(b.required):
        out.append(f"{label} required: {sorted(a.required)} -> {sorted(b.required)}")
    return out


def diff_artifacts(a: Artifact, b: Artifact) -> ArtifactDiff:
    d = ArtifactDiff(a.capability_id, a.version, b.version)
    for label, x, y in (
        ("discovered_by", a.provenance.discovered_by, b.provenance.discovered_by),
        ("reviewed", a.provenance.reviewed, b.provenance.reviewed),
        ("approved_by", a.provenance.approved_by, b.provenance.approved_by),
        ("change_note", a.provenance.change_note, b.provenance.change_note),
        ("target.base_url", a.target.base_url, b.target.base_url),
    ):
        if x != y:
            d.metadata.append(f"{label}: {x!r} -> {y!r}")
    d.schema = _diff_props("input", a.input_schema, b.input_schema) + _diff_props("output", a.output_schema, b.output_schema)
    if a.safety != b.safety:
        d.safety.append(f"safety: {a.safety.model_dump()} -> {b.safety.model_dump()}")

    sigs_a = [f"{s.action.value}|{s.target.semantic_description if s.target else s.params.get('url', '')}" for s in a.steps]
    sigs_b = [f"{s.action.value}|{s.target.semantic_description if s.target else s.params.get('url', '')}" for s in b.steps]
    for op, i1, i2, j1, j2 in SequenceMatcher(None, sigs_a, sigs_b, autojunk=False).get_opcodes():
        if op == "equal":
            for sa, sb in zip(a.steps[i1:i2], b.steps[j1:j2], strict=True):
                if details := _step_details(sa, sb):
                    d.steps.append(StepChange("changed", sb.step_id, _describe(sb), details))
        elif op == "replace":
            for sa, sb in zip(a.steps[i1:i2], b.steps[j1:j2], strict=False):
                d.steps.append(StepChange("changed", sb.step_id, f"{_describe(sa)} -> {_describe(sb)}", _step_details(sa, sb)))
            for sa in a.steps[i1 + (j2 - j1):i2]:
                d.steps.append(StepChange("removed", sa.step_id, _describe(sa)))
            for sb in b.steps[j1 + (i2 - i1):j2]:
                d.steps.append(StepChange("added", sb.step_id, _describe(sb)))
        elif op == "delete":
            d.steps += [StepChange("removed", s.step_id, _describe(s)) for s in a.steps[i1:i2]]
        elif op == "insert":
            d.steps += [StepChange("added", s.step_id, _describe(s)) for s in b.steps[j1:j2]]

    def outcomes(x: Artifact) -> set[str]:
        return {f"{r.signal.type.value}={r.signal.value!r} -> {r.output_field}={r.outcome}" for r in x.error_handling.business_outcomes}

    def recoverables(x: Artifact) -> set[str]:
        return {f"{r.signal.type.value}={r.signal.value!r} -> {r.action.value}" for r in x.error_handling.recoverable}

    for label, fn in (("business outcome", outcomes), ("recoverable", recoverables)):
        ca, cb = fn(a), fn(b)
        d.error_handling += [f"+ {label}: {x}" for x in sorted(cb - ca)] + [f"- {label}: {x}" for x in sorted(ca - cb)]
    return d
