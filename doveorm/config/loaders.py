"""Read settings from environment variables or a Python settings file."""
from __future__ import annotations

import importlib.util
import json
import os
from typing import Any

from ..exceptions import ConfigurationError
from .defaults import DEFAULTS


def load_env_settings(prefix: str = "DOVEORM_") -> dict[str, Any]:
    """Collect ``{prefix}{SETTING}`` environment variables into a settings dict.

    Booleans and integers are coerced; dict-valued settings
    (``DATABASE_OPTIONS``, ``DATABASES``) are parsed as JSON.
    """
    overrides: dict[str, Any] = {}
    for key, default in DEFAULTS.items():
        env_key = f"{prefix}{key}"
        val = os.environ.get(env_key)
        if val is None:
            continue
        if isinstance(default, bool):
            lowered = val.strip().lower()
            if lowered in ("1", "true", "yes", "on"):
                overrides[key] = True
            elif lowered in ("0", "false", "no", "off", ""):
                overrides[key] = False
            else:
                raise ConfigurationError(f"{env_key} must be a boolean, got {val!r}.")
        elif isinstance(default, int):
            try:
                overrides[key] = int(val)
            except ValueError:
                raise ConfigurationError(f"{env_key} must be an integer, got {val!r}.") from None
        elif isinstance(default, dict) or key == "DATABASES":
            try:
                parsed = json.loads(val)
            except ValueError as exc:
                raise ConfigurationError(f"{env_key} must be valid JSON: {exc}") from None
            if parsed is not None and not isinstance(parsed, dict):
                raise ConfigurationError(f"{env_key} must be a JSON object.")
            overrides[key] = parsed
        else:
            overrides[key] = val
    return overrides


def load_file_settings(filepath: str) -> dict[str, Any]:
    """Execute a Python settings file and collect known setting names.

    .. warning:: The file is *executed*.  Only pass trusted paths.
    """
    if not os.path.isfile(filepath):
        raise ConfigurationError(f"Settings file not found: {filepath!r}")
    spec = importlib.util.spec_from_file_location("_doveorm_settings", filepath)
    if spec is None or spec.loader is None:
        raise ConfigurationError(f"Cannot load settings file: {filepath!r}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    overrides: dict[str, Any] = {}
    for key in DEFAULTS:
        if hasattr(mod, key):
            overrides[key] = getattr(mod, key)
    return overrides
