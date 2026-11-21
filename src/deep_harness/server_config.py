"""Shared process-wide server defaults and per-launch configuration readers."""

import math
import os
import tomllib
from importlib.resources import files
from pathlib import Path

import yaml


def _sections(data):
    if data is None:
        return {}
    if not isinstance(data, dict) or any(
        not isinstance(name, str)
        or not isinstance(values, dict)
        or any(not isinstance(key, str) for key in values)
        for name, values in data.items()
    ):
        raise ValueError("Server configuration must be a mapping of named sections")
    return data


def _merge(defaults, overrides):
    result = {name: values.copy() for name, values in defaults.items()}
    for name, values in overrides.items():
        if name not in result or values.keys() - result[name].keys():
            raise ValueError(f"Unknown shared server settings in section {name!r}")
        for key, value in values.items():
            original = defaults[name][key]
            if (name, key) == ("model", "base_url") and value is None:
                continue
            if type(original) in (str, bool) and type(value) is not type(original):
                raise ValueError(f"Invalid type for shared server setting {name}.{key}")
            if isinstance(original, list) and (
                not isinstance(value, list) or not all(isinstance(item, str) for item in value)
            ):
                raise ValueError(f"Shared server setting {name}.{key} requires a string list")
            if original is None and value is not None and not isinstance(value, str):
                raise ValueError(f"Shared server setting {name}.{key} requires a string or null")
            if type(original) is int and type(value) is not int:
                raise ValueError(f"Shared server setting {name}.{key} requires an integer")
            if type(original) in (int, float) and (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or value < 0
                or (original > 0 and value == 0)
            ):
                raise ValueError(f"Invalid numeric setting {name}.{key}")
        result[name].update(values)
    return result


def _load_defaults():
    path = files(__package__) / "server.yaml"
    defaults = _sections(yaml.safe_load(path.read_text(encoding="utf-8")))
    override = os.environ.get("DEEP_HARNESS_SERVER_CONFIG")
    if not override:
        return defaults
    return _merge(defaults, _sections(yaml.safe_load(Path(override).read_text(encoding="utf-8"))))


_DEFAULTS = _load_defaults()


def setting(path: str):
    section, key = path.split(".", 1)
    return _DEFAULTS[section][key]


def number(path: str) -> int | float:
    value = setting(path)
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"Server value {path!r} must be a non-negative finite number")
    return value


def defaults_for(section: str, keys) -> dict:
    return {key: value for key, value in _DEFAULTS[section].items() if key in keys}


def read_operator_config(path: Path | None) -> dict:
    if path is None:
        return {}
    if path.suffix.lower() in {".yaml", ".yml"}:
        return _sections(yaml.safe_load(path.read_text(encoding="utf-8")))
    with path.open("rb") as stream:
        return _sections(tomllib.load(stream))
