"""
JSON Schema response validation engine for llm-proxy.

Validates LLM responses against a JSON Schema using stdlib only (no external deps).
Supports: required fields, type checks, enum values, string patterns, array item validation.
"""
from __future__ import annotations

import json
import re
from typing import Any, List, Optional, Tuple


def validate_response(response_text: str, schema_path: str) -> Tuple[bool, List[str]]:
    """
    Validate an LLM response string against a JSON Schema file.

    Args:
        response_text: The raw text response from the LLM.
        schema_path: Path to a JSON Schema file.

    Returns:
        (is_valid, violations) where violations is a list of human-readable strings.
    """
    # Load schema
    try:
        with open(schema_path) as f:
            schema = json.load(f)
    except FileNotFoundError:
        return False, ["Schema file not found: {}".format(schema_path)]
    except json.JSONDecodeError as exc:
        return False, ["Invalid JSON in schema file: {}".format(exc)]

    # Parse response as JSON
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as exc:
        return False, ["Response is not valid JSON: {}".format(exc)]

    violations: List[str] = []
    _validate_value(data, schema, "$", violations)
    return len(violations) == 0, violations


def _validate_value(
    value: Any,
    schema: dict,
    path: str,
    violations: List[str],
) -> None:
    """Recursively validate a value against a schema node."""
    # Handle $ref (basic: only fragment refs like #/definitions/Foo)
    if "$ref" in schema:
        # We don't resolve full refs without the root schema context here;
        # skip unresolvable refs silently.
        return

    # Type check
    if "type" in schema:
        expected_type = schema["type"]
        if not _check_type(value, expected_type):
            violations.append(
                "{}: expected type '{}', got '{}'".format(
                    path, expected_type, _json_type(value)
                )
            )
            # Don't recurse if type is wrong — sub-checks won't make sense
            return

    # Enum check
    if "enum" in schema:
        if value not in schema["enum"]:
            violations.append(
                "{}: value {} not in enum {}".format(path, json.dumps(value), json.dumps(schema["enum"]))
            )

    # const check
    if "const" in schema:
        if value != schema["const"]:
            violations.append(
                "{}: expected const {}, got {}".format(
                    path, json.dumps(schema["const"]), json.dumps(value)
                )
            )

    # String-specific checks
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            violations.append(
                "{}: string length {} < minLength {}".format(path, len(value), schema["minLength"])
            )
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            violations.append(
                "{}: string length {} > maxLength {}".format(path, len(value), schema["maxLength"])
            )
        if "pattern" in schema:
            if not re.search(schema["pattern"], value):
                violations.append(
                    "{}: string '{}' does not match pattern '{}'".format(
                        path, value, schema["pattern"]
                    )
                )

    # Number-specific checks
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            violations.append("{}: {} < minimum {}".format(path, value, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            violations.append("{}: {} > maximum {}".format(path, value, schema["maximum"]))
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            violations.append(
                "{}: {} <= exclusiveMinimum {}".format(path, value, schema["exclusiveMinimum"])
            )
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            violations.append(
                "{}: {} >= exclusiveMaximum {}".format(path, value, schema["exclusiveMaximum"])
            )
        if "multipleOf" in schema:
            multiple = schema["multipleOf"]
            if multiple != 0 and (value % multiple) != 0:
                violations.append("{}: {} is not a multiple of {}".format(path, value, multiple))

    # Object checks
    if isinstance(value, dict):
        # Required fields
        for req_field in schema.get("required", []):
            if req_field not in value:
                violations.append("{}: missing required field '{}'".format(path, req_field))

        # Property schemas
        properties = schema.get("properties", {})
        for prop_name, prop_schema in properties.items():
            if prop_name in value:
                _validate_value(
                    value[prop_name],
                    prop_schema,
                    "{}.{}".format(path, prop_name),
                    violations,
                )

        # additionalProperties: false
        additional = schema.get("additionalProperties")
        if additional is False:
            known_props = set(schema.get("properties", {}).keys())
            for key in value:
                if key not in known_props:
                    violations.append(
                        "{}: additional property '{}' not allowed".format(path, key)
                    )
        elif isinstance(additional, dict):
            known_props = set(schema.get("properties", {}).keys())
            for key, val in value.items():
                if key not in known_props:
                    _validate_value(
                        val, additional, "{}.{}".format(path, key), violations
                    )

        # minProperties / maxProperties
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            violations.append(
                "{}: object has {} properties < minProperties {}".format(
                    path, len(value), schema["minProperties"]
                )
            )
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            violations.append(
                "{}: object has {} properties > maxProperties {}".format(
                    path, len(value), schema["maxProperties"]
                )
            )

    # Array checks
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            violations.append(
                "{}: array has {} items < minItems {}".format(path, len(value), schema["minItems"])
            )
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            violations.append(
                "{}: array has {} items > maxItems {}".format(path, len(value), schema["maxItems"])
            )

        # items schema
        if "items" in schema:
            items_schema = schema["items"]
            if isinstance(items_schema, dict):
                for i, item in enumerate(value):
                    _validate_value(item, items_schema, "{}[{}]".format(path, i), violations)
            elif isinstance(items_schema, list):
                # Tuple validation
                for i, (item, item_schema) in enumerate(zip(value, items_schema)):
                    _validate_value(item, item_schema, "{}[{}]".format(path, i), violations)

        # uniqueItems
        if schema.get("uniqueItems"):
            seen = []
            for item in value:
                try:
                    serialized = json.dumps(item, sort_keys=True)
                except (TypeError, ValueError):
                    serialized = str(item)
                if serialized in seen:
                    violations.append("{}: array items must be unique".format(path))
                    break
                seen.append(serialized)

    # anyOf / oneOf / allOf (basic support)
    if "allOf" in schema:
        for sub_schema in schema["allOf"]:
            _validate_value(value, sub_schema, path, violations)

    if "anyOf" in schema:
        sub_violations_list = []
        any_valid = False
        for sub_schema in schema["anyOf"]:
            sub_v: List[str] = []
            _validate_value(value, sub_schema, path, sub_v)
            if not sub_v:
                any_valid = True
                break
            sub_violations_list.append(sub_v)
        if not any_valid:
            all_sub = [v for sv in sub_violations_list for v in sv]
            violations.append(
                "{}: value did not match any of the anyOf schemas: {}".format(
                    path, "; ".join(all_sub[:3])
                )
            )

    if "oneOf" in schema:
        matching = 0
        for sub_schema in schema["oneOf"]:
            sub_v: List[str] = []
            _validate_value(value, sub_schema, path, sub_v)
            if not sub_v:
                matching += 1
        if matching != 1:
            violations.append(
                "{}: value must match exactly one of the oneOf schemas (matched {})".format(
                    path, matching
                )
            )


def _check_type(value: Any, expected: str) -> bool:
    """Check if value matches the JSON Schema type string."""
    if expected == "string":
        return isinstance(value, str)
    elif expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    elif expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    elif expected == "boolean":
        return isinstance(value, bool)
    elif expected == "null":
        return value is None
    elif expected == "array":
        return isinstance(value, list)
    elif expected == "object":
        return isinstance(value, dict)
    return True  # Unknown type — don't reject


def _json_type(value: Any) -> str:
    """Return the JSON Schema type name for a Python value."""
    if isinstance(value, bool):
        return "boolean"
    elif isinstance(value, int):
        return "integer"
    elif isinstance(value, float):
        return "number"
    elif isinstance(value, str):
        return "string"
    elif value is None:
        return "null"
    elif isinstance(value, list):
        return "array"
    elif isinstance(value, dict):
        return "object"
    return type(value).__name__
