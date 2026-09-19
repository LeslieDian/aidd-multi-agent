"""Recursive JSON tool validation including nested objects, arrays and finite numbers."""
import json
import math


def validate_value(value, spec, path):
    kind = spec["type"]
    if kind == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")
        props = spec.get("properties", {})
        if set(spec.get("required", props)) - set(value) or (spec.get("additionalProperties", False) is False and set(value) - set(props)):
            raise ValueError(f"{path}: Expected arguments: {list(props)}")
        for key, item in value.items():
            if key in props:
                validate_value(item, props[key], f"{path}.{key}")
    elif kind == "array":
        if not isinstance(value, list) or not spec.get("minItems", 0) <= len(value) <= spec.get("maxItems", 20):
            raise ValueError(f"{path} must contain {spec.get('minItems', 0)}-{spec.get('maxItems', 20)} items")
        if spec.get("uniqueItems") and len({json.dumps(x, sort_keys=True) for x in value}) != len(value):
            raise ValueError(f"{path} requires unique items")
        for index, item in enumerate(value):
            validate_value(item, spec["items"], f"{path}[{index}]")
    elif kind == "string":
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{path} must be a nonempty string")
    elif kind in {"integer", "number"}:
        if (type(value) not in ((int,) if kind == "integer" else (int, float)) or not math.isfinite(value)
                or value < spec.get("minimum", -math.inf) or value > spec.get("maximum", math.inf)):
            raise ValueError(f"{path} outside permitted {kind} range: minimum={spec.get('minimum', '-infinity')}, maximum={spec.get('maximum', 'infinity')}; got {value!r}")
    else:
        raise ValueError(f"Unsupported schema type: {kind}")
    if "enum" in spec and value not in spec["enum"]:
        raise ValueError(f"{path} must be one of {spec['enum']}")
