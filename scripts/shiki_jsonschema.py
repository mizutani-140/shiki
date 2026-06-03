"""Bounded JSON Schema validation for Shiki-owned contracts."""

from __future__ import annotations

import re
from typing import Any


class JsonSchemaError(ValueError):
    """Raised when data does not satisfy a supported Shiki schema."""


class UnsupportedJsonSchemaError(JsonSchemaError):
    """Raised when a schema uses a feature outside Shiki's bounded subset."""


UNSUPPORTED_KEYWORDS = {
    "$ref",
    "oneOf",
    "anyOf",
    "allOf",
    "format",
    "dependencies",
    "if",
    "then",
    "else",
}

SUPPORTED_KEYWORDS = {
    "$schema",
    "$id",
    "title",
    "description",
    "type",
    "required",
    "properties",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "minItems",
    "maxItems",
    "pattern",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
}


def assert_supported_schema(schema: Any, path: str = "$") -> None:
    if not isinstance(schema, dict):
        raise UnsupportedJsonSchemaError(f"{path}: schema must be an object")
    for key, value in schema.items():
        if key in UNSUPPORTED_KEYWORDS:
            raise UnsupportedJsonSchemaError(f"{path}: unsupported JSON Schema keyword {key!r}")
        if key not in SUPPORTED_KEYWORDS:
            raise UnsupportedJsonSchemaError(f"{path}: unsupported JSON Schema keyword {key!r}")
        if key == "properties":
            if not isinstance(value, dict):
                raise UnsupportedJsonSchemaError(f"{path}.properties must be an object")
            for property_name, property_schema in value.items():
                assert_supported_schema(property_schema, f"{path}.properties.{property_name}")
        if key == "items":
            assert_supported_schema(value, f"{path}.items")


def _json_type_matches(expected_type: str, instance: Any) -> bool:
    if expected_type == "object":
        return isinstance(instance, dict)
    if expected_type == "array":
        return isinstance(instance, list)
    if expected_type == "string":
        return isinstance(instance, str)
    if expected_type == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected_type == "number":
        return (isinstance(instance, int) or isinstance(instance, float)) and not isinstance(instance, bool)
    if expected_type == "boolean":
        return isinstance(instance, bool)
    if expected_type == "null":
        return instance is None
    raise UnsupportedJsonSchemaError(f"unsupported JSON Schema type {expected_type!r}")


def validate_json_schema(instance: Any, schema: dict[str, Any], path: str = "$") -> None:
    assert_supported_schema(schema)

    expected = schema.get("type")
    if expected is not None:
        expected_types = [expected] if isinstance(expected, str) else expected
        if not isinstance(expected_types, list) or not all(isinstance(item, str) for item in expected_types):
            raise UnsupportedJsonSchemaError(f"{path}: type must be a string or list of strings")
        if not any(_json_type_matches(item, instance) for item in expected_types):
            raise JsonSchemaError(f"{path}: expected type {expected_types}, got {type(instance).__name__}")

    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list):
            raise UnsupportedJsonSchemaError(f"{path}: enum must be a list")
        if instance not in enum:
            raise JsonSchemaError(f"{path}: expected one of {enum!r}, got {instance!r}")

    if "const" in schema and instance != schema["const"]:
        raise JsonSchemaError(f"{path}: expected const {schema['const']!r}, got {instance!r}")

    if isinstance(instance, dict):
        required = schema.get("required", [])
        if required is None:
            required = []
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise UnsupportedJsonSchemaError(f"{path}: required must be a list of strings")
        for key in required:
            if key not in instance:
                raise JsonSchemaError(f"{path}: missing required property {key!r}")

        properties = schema.get("properties", {})
        if properties is None:
            properties = {}
        if not isinstance(properties, dict):
            raise UnsupportedJsonSchemaError(f"{path}: properties must be an object")
        for key, property_schema in properties.items():
            if key in instance:
                validate_json_schema(instance[key], property_schema, f"{path}.{key}")

        additional = schema.get("additionalProperties", True)
        if additional is False:
            allowed = set(properties)
            extras = sorted(set(instance) - allowed)
            if extras:
                raise JsonSchemaError(f"{path}: unexpected additional properties: {', '.join(extras)}")
        elif isinstance(additional, dict):
            for key in set(instance) - set(properties):
                validate_json_schema(instance[key], additional, f"{path}.{key}")
        elif additional is not True:
            raise UnsupportedJsonSchemaError(f"{path}: additionalProperties must be boolean or schema")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < int(schema["minItems"]):
            raise JsonSchemaError(f"{path}: expected at least {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > int(schema["maxItems"]):
            raise JsonSchemaError(f"{path}: expected at most {schema['maxItems']} items")
        if "items" in schema:
            items_schema = schema["items"]
            if not isinstance(items_schema, dict):
                raise UnsupportedJsonSchemaError(f"{path}: items must be a schema object")
            for index, item in enumerate(instance):
                validate_json_schema(item, items_schema, f"{path}[{index}]")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < int(schema["minLength"]):
            raise JsonSchemaError(f"{path}: expected string length >= {schema['minLength']}")
        if "maxLength" in schema and len(instance) > int(schema["maxLength"]):
            raise JsonSchemaError(f"{path}: expected string length <= {schema['maxLength']}")
        if "pattern" in schema and re.search(str(schema["pattern"]), instance) is None:
            raise JsonSchemaError(f"{path}: value {instance!r} does not match pattern {schema['pattern']!r}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise JsonSchemaError(f"{path}: expected value >= {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            raise JsonSchemaError(f"{path}: expected value <= {schema['maximum']}")
