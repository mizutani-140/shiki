"""Small dependency-free JSON Schema subset validator for Shiki contracts."""

from __future__ import annotations

import re
from typing import Any


class SchemaValidationError(ValueError):
    """Raised when a JSON value does not satisfy the supported schema subset."""


def validate_instance(instance: Any, schema: dict[str, Any], *, path: str = "$") -> None:
    """Validate `instance` against the JSON Schema subset Shiki uses locally."""
    schema_type = schema.get("type")
    if schema_type is not None and not _matches_type(instance, schema_type):
        raise SchemaValidationError(f"{path}: expected type {_type_label(schema_type)}, got {_json_type(instance)}")

    if "enum" in schema and instance not in schema["enum"]:
        raise SchemaValidationError(f"{path}: value {instance!r} is not one of {schema['enum']!r}")

    if isinstance(instance, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(instance) < min_length:
            raise SchemaValidationError(f"{path}: string length must be >= {min_length}")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, instance) is None:
            raise SchemaValidationError(f"{path}: string does not match pattern {pattern!r}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and instance < minimum:
            raise SchemaValidationError(f"{path}: number must be >= {minimum}")
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and instance > maximum:
            raise SchemaValidationError(f"{path}: number must be <= {maximum}")

    if isinstance(instance, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(instance) < min_items:
            raise SchemaValidationError(f"{path}: array length must be >= {min_items}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                validate_instance(item, item_schema, path=f"{path}[{index}]")

    if isinstance(instance, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if key not in instance:
                    raise SchemaValidationError(f"{path}: missing required property {key!r}")

        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, property_schema in properties.items():
                if key in instance and isinstance(property_schema, dict):
                    validate_instance(instance[key], property_schema, path=f"{path}.{key}")

            additional = schema.get("additionalProperties", True)
            if additional is False:
                extra = sorted(set(instance) - set(properties))
                if extra:
                    raise SchemaValidationError(f"{path}: additional properties are not allowed: {extra!r}")
            elif isinstance(additional, dict):
                for key in sorted(set(instance) - set(properties)):
                    validate_instance(instance[key], additional, path=f"{path}.{key}")


def _matches_type(value: Any, schema_type: str | list[str]) -> bool:
    expected = [schema_type] if isinstance(schema_type, str) else schema_type
    return any(_matches_single_type(value, item) for item in expected)


def _matches_single_type(value: Any, schema_type: str) -> bool:
    if schema_type == "null":
        return value is None
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "object":
        return isinstance(value, dict)
    return False


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _type_label(schema_type: str | list[str]) -> str:
    if isinstance(schema_type, list):
        return "|".join(schema_type)
    return schema_type
