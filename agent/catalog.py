"""A small registry of known capability contracts -- what a human decides discovery should
produce (capability_id, typed input/output, success/error signals, target) before the agent
figures out how. See agent/recorder.py's docstring for why this split exists: a human
specifies the contract, the model figures out the implementation.

Error handling is shared per target rather than re-discovered per capability: a target's known
failure modes are curated once, the same way a real system would maintain a reviewed library of
known signatures for a given app instead of having an agent reinvent them for every capability
it learns.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from artifacts_lib.schema import (
    BusinessOutcomeRule,
    CanarySpec,
    CapabilityRiskLevel,
    CapabilityTarget,
    ErrorHandling,
    JSONSchemaObject,
    Locator,
    LocatorStrategy,
    Preconditions,
    RecoverableRule,
    RecoveryAction,
    SafetyMeta,
    Signal,
    SignalType,
    SurfaceType,
    Target,
)


@dataclass
class CapabilitySpec:
    capability_id: str
    version: str
    name: str
    description: str
    goal: str
    start_path: str
    target: CapabilityTarget
    input_schema: JSONSchemaObject
    output_schema: JSONSchemaObject
    success_checkpoint: Signal
    error_handling: ErrorHandling
    safety: SafetyMeta
    preconditions: Preconditions | None = None
    success_output_defaults: dict[str, str] = field(default_factory=dict)
    canary: CanarySpec | None = None


def known_capabilities() -> list[str]:
    from agent.catalog_clinic import CLINIC_CATALOG

    return sorted(CLINIC_CATALOG)


def get_spec(capability_id: str, base_url: str) -> CapabilitySpec:
    from agent.catalog_clinic import CLINIC_CATALOG

    factory = CLINIC_CATALOG.get(capability_id)
    if factory is None:
        raise KeyError(f"unknown capability '{capability_id}' -- known: {known_capabilities()}")
    return factory(base_url)
