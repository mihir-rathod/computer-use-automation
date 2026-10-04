"""Validates caller-supplied inputs against an artifact's input_schema before replay starts --
a malformed call should fail fast with a clear message, not partway through a live run.
"""
from __future__ import annotations

import re
from typing import Any

from artifacts_lib.schema import JSONSchemaObject


def validate_input(schema: JSONSchemaObject, inputs: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in schema.required:
        if field not in inputs:
            errors.append(f"missing required input '{field}'")

    if schema.at_least_one_of and not any(inputs.get(f) is not None for f in schema.at_least_one_of):
        errors.append(f"provide at least one of: {', '.join(schema.at_least_one_of)}")

    for key, value in inputs.items():
        prop = schema.properties.get(key)
        if prop is None:
            continue  # extra inputs are ignored, not an error -- forward-compatible callers
        if value is None and key not in schema.required:
            continue  # null on an optional input means "not supplied"
        declared_type = prop.get("type")
        if declared_type and not _type_ok(value, declared_type):
            errors.append(f"input '{key}' should be of type {declared_type}, got {type(value).__name__}")
            continue
        pattern = prop.get("pattern")
        if pattern and isinstance(value, str) and not re.match(pattern, value):
            errors.append(f"input '{key}' does not match required pattern {pattern!r}")
        allowed = prop.get("enum")
        if allowed is not None and value not in allowed:
            errors.append(f"input '{key}' must be one of {allowed}, got {value!r}")
        if isinstance(value, str):
            if "minLength" in prop and len(value) < prop["minLength"]:
                errors.append(f"input '{key}' is shorter than {prop['minLength']} characters")
            if "maxLength" in prop and len(value) > prop["maxLength"]:
                errors.append(f"input '{key}' is longer than {prop['maxLength']} characters")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in prop and value < prop["minimum"]:
                errors.append(f"input '{key}' must be at least {prop['minimum']}")
            if "maximum" in prop and value > prop["maximum"]:
                errors.append(f"input '{key}' must be at most {prop['maximum']}")

    return errors


def _type_ok(value: Any, declared_type: Any) -> bool:
    types = declared_type if isinstance(declared_type, list) else [declared_type]
    for t in types:
        if t == "null":
            if value is None:
                return True
            continue
        if t == "boolean":
            if isinstance(value, bool):
                return True
            continue
        if isinstance(value, bool):
            continue  # bool is a subclass of int -- never satisfies integer/number
        if t == "integer" and isinstance(value, int):
            return True
        if t == "number" and isinstance(value, (int, float)):
            return True
        if t == "string" and isinstance(value, str):
            return True
        if t == "object" and isinstance(value, dict):
            return True
        if t == "array" and isinstance(value, list):
            return True
    return False
