"""The JSON Schema an editor completes `.claude/orchestra.json` from describes exactly what `config.py` reads.

The key tree, each key's type, whether it is required and its default are derived from both sides
and compared, so a key added to one side alone fails here.
"""

from __future__ import annotations

import dataclasses
import json
import types
import typing
from pathlib import Path
from typing import Any, Literal

from orchestra import config

SCHEMA = Path(__file__).resolve().parents[1] / "schema" / "orchestra.schema.json"


def _json(value: object) -> object:
    """A default as the file would spell it."""
    if dataclasses.is_dataclass(value):
        return {}
    if isinstance(value, tuple):
        return [_json(item) for item in typing.cast("tuple[object, ...]", value)]
    return value


def _from_type(hint: object) -> dict[str, Any]:
    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        nodes = [_from_type(arg) for arg in typing.get_args(hint)]
        if any(node["type"] == "enum" for node in nodes):
            # The schema spells a choice that may be null as one enum holding null.
            values = [value for node in nodes for value in node.get("values", [json.dumps(None)])]
            return {"type": "enum", "values": sorted(values)}
        # An object that may be null keeps its keys.
        keys = [node["keys"] for node in nodes if "keys" in node]
        return {"type": sorted(node["type"] for node in nodes)} | ({"keys": keys[0]} if keys else {})
    if origin is tuple:
        return {"type": "array", "items": _from_type(typing.get_args(hint)[0])}
    if origin is dict:
        return {"type": "map", "values": _from_type(typing.get_args(hint)[1])}
    if origin is Literal:
        return {"type": "enum", "values": sorted(json.dumps(value) for value in typing.get_args(hint))}
    if dataclasses.is_dataclass(hint):
        return {"type": "object", "keys": _from_class(typing.cast("type", hint))}
    return {"type": {str: "string", int: "integer", float: "number", type(None): "null"}[typing.cast("type", hint)]}


def _from_class(cls: type) -> dict[str, Any]:
    hints = typing.get_type_hints(cls)
    tree: dict[str, Any] = {}
    for spec in dataclasses.fields(cls):
        node = _from_type(hints[spec.name])
        if spec.default is not dataclasses.MISSING:
            if dataclasses.is_dataclass(spec.default):
                # A nested object's default is its own defaults, which the schema spells `{}`.
                assert spec.default == hints[spec.name]()
            node["default"] = _json(spec.default)
        elif spec.default_factory is not dataclasses.MISSING:
            node["default"] = _json(spec.default_factory())
        else:
            node["required"] = True
        if spec.metadata.get(config.NONEMPTY):
            node["minItems"] = 1
        if spec.metadata.get(config.POSITIVE):
            node["exclusiveMinimum"] = 0
        if spec.metadata.get(config.LITERAL):
            node["items"]["pattern"] = "^[^*]*$"
        tree[config.json_key(spec)] = node
    return tree


def _from_schema(node: dict[str, Any]) -> dict[str, Any]:
    if "enum" in node:
        return {"type": "enum", "values": sorted(json.dumps(value) for value in node["enum"])}
    kind = node["type"]
    if isinstance(kind, list):
        held = {"type": sorted(typing.cast("list[str]", kind))}
        return held | ({"keys": _keys(node)} if "properties" in node else {})
    if kind == "array":
        items = _from_schema(node["items"]) | (
            {"pattern": node["items"]["pattern"]} if "pattern" in node["items"] else {}
        )
        return {"type": "array", "items": items}
    if kind == "object" and isinstance(node.get("additionalProperties"), dict):
        return {"type": "map", "values": _from_schema(node["additionalProperties"])}
    if kind == "object":
        assert node.get("additionalProperties") is False, node
        return {"type": "object", "keys": _keys(node)}
    return {"type": kind}


def _keys(node: dict[str, Any]) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    required = set(node.get("required", []))
    for key, held in node["properties"].items():
        tree[key] = _from_schema(held)
        if key in required:
            tree[key]["required"] = True
        if "default" in held:
            tree[key]["default"] = held["default"]
        for bound in ("minItems", "exclusiveMinimum"):
            if bound in held:
                tree[key][bound] = held[bound]
    return tree


def test_the_schema_holds_every_key_type_and_default_the_configuration_reads() -> None:
    schema = json.loads(SCHEMA.read_bytes())
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"].endswith("/schema/orchestra.schema.json")
    assert _from_schema(schema) == {"type": "object", "keys": _from_class(config.Config)}
