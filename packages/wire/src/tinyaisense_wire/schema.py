"""A validator for the subset of JSON Schema that the Kind contracts use."""

import functools
import re
from collections.abc import Mapping
from typing import Any, cast

KEYWORDS = frozenset(
    {
        "type",
        "enum",
        "minimum",
        "maximum",
        "pattern",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "default",
    }
)
"""The keywords `validate` understands. `default` is an annotation only."""

_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "boolean": (bool,),
    "null": (type(None),),
    "integer": (int,),
    "number": (int, float),
}


class SchemaError(ValueError):
    """A value breaks its schema. The hub and the Simulator answer it with `invalid_args`."""

    def __init__(self, where: str, problem: str) -> None:
        super().__init__(f"{where} {problem}")
        self.where = where
        self.problem = problem


def validate(schema: Mapping[str, Any], value: object, where: str = "value") -> None:
    """Raise `SchemaError`, naming the offending path, if `value` breaks `schema`.

    Unknown object keys are allowed unless `additionalProperties` says otherwise: receivers
    ignore unknown fields. Booleans are never integers, and `1.0` is not an integer.
    """
    if "type" in schema:
        names: list[str] = [schema["type"]] if isinstance(schema["type"], str) else schema["type"]
        if not any(type(value) in _TYPES[name] for name in names):
            raise SchemaError(where, f"must be {' or '.join(names)}")
    if "enum" in schema and not any(
        type(value) is type(option) and value == option for option in schema["enum"]
    ):
        raise SchemaError(where, f"must be one of {schema['enum']}")
    if type(value) in (int, float):
        number = cast(float, value)
        if "minimum" in schema and number < schema["minimum"]:
            raise SchemaError(where, f"must be at least {schema['minimum']}")
        if "maximum" in schema and number > schema["maximum"]:
            raise SchemaError(where, f"must be at most {schema['maximum']}")
    if (
        isinstance(value, str)
        and "pattern" in schema
        and not _pattern(schema["pattern"]).search(value)
    ):
        raise SchemaError(where, f"must match {schema['pattern']}")
    if isinstance(value, dict):
        _validate_object(schema, cast(dict[str, object], value), where)
    if isinstance(value, list):
        items = cast(list[object], value)
        if len(items) < schema.get("minItems", 0):
            raise SchemaError(where, f"must have at least {schema['minItems']} items")
        if "items" in schema:
            for i, item in enumerate(items):
                validate(schema["items"], item, f"{where}[{i}]")


def _validate_object(schema: Mapping[str, Any], obj: dict[str, object], where: str) -> None:
    for key in schema.get("required", ()):
        if key not in obj:
            raise SchemaError(f"{where}.{key}", "is missing")
    properties: Mapping[str, Any] = schema.get("properties", {})
    additional: bool | Mapping[str, Any] = schema.get("additionalProperties", True)
    for key, item in obj.items():
        if key in properties:
            validate(properties[key], item, f"{where}.{key}")
        elif additional is False:
            raise SchemaError(f"{where}.{key}", "isn't allowed")
        elif additional is not True:
            validate(additional, item, f"{where}.{key}")


@functools.cache
def _pattern(pattern: str) -> re.Pattern[str]:
    # JSON Schema patterns are ECMA regexes, where `$` means the end of the input. Python's
    # `$` also matches before a trailing newline, so anchor with `\Z` instead.
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        pattern = pattern[:-1] + r"\Z"
    return re.compile(pattern)


def check_schema(schema: Mapping[str, Any], where: str = "schema") -> None:
    """Raise `ValueError` if `schema` uses a keyword `validate` doesn't understand."""
    if unknown := set(schema) - KEYWORDS:
        raise ValueError(f"{where} uses unsupported keywords {sorted(unknown)}")
    for key, sub in schema.get("properties", {}).items():
        check_schema(sub, f"{where}.properties.{key}")
    for keyword in ("items", "additionalProperties"):
        if isinstance(sub := schema.get(keyword), Mapping):
            check_schema(cast(Mapping[str, Any], sub), f"{where}.{keyword}")
