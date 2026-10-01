"""Config — single centralized config file.

Spec (Configuration section):
- Location: --config <path> flag; default = <project root>/config.json
- Auto-generated with all defaults if no config file is found at that location
- --generate-config emits all keys with defaults + descriptions
- Precedence: config file > built-in default
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "config.json"

# Default config location: the project root (the directory containing src/).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "listen": "127.0.0.1:8080",
    "upstream": "127.0.0.1:5000",
    "save_path": "",
    "min_save_tokens": 512,
    "n_max_files": 4,
    "max_gb": 2,
    "thrash_window": 8,
    "thrash_max_switches": 1,
    "tail_match_min": 64,
    "cache_reuse": 0,
    "cache_reuse_mode": "targeted",
    "health_poll_ms": 3000,
    "control_timeout_ms": 10000,
    "api_key": "",
}

# Key descriptions for --generate-config output.
DESCRIPTIONS: dict[str, str] = {
    "listen": "Proxy bind address (host:port)",
    "upstream": "llama-server (router) address (host:port)",
    "save_path": "Where .bin files live; MUST match the server's --slot-save-path",
    "min_save_tokens": "Skip saving below this token length (>= 0)",
    "n_max_files": "Max .bin files on shelf (>= 0; 0 = unlimited)",
    "max_gb": "Max total shelf size in GB (>= 0; 0 = unlimited)",
    "thrash_window": "Rolling request window for the thrashing guard K (>= 2)",
    "thrash_max_switches": "Switches allowed per window before save+restore pause (>= 0)",
    "tail_match_min": "Min tail overlap for shifted-suffix detection (>= 1)",
    "cache_reuse": "n_cache_reuse min chunk size to inject on a shifted-suffix (>= 0; 0 = off)",
    "cache_reuse_mode": "When to inject n_cache_reuse: targeted | always",
    "health_poll_ms": "/health poll interval for router-restart detection (> 0)",
    "control_timeout_ms": "Timeout for proxy-issued save/restore/input_tokens calls (> 0)",
    "api_key": "Server's --api-key, carried on control calls",
}

# Schema: each key's type/constraint/enum (written as _schema in config.json).
SCHEMA: dict[str, str] = {
    "listen": "host:port",
    "upstream": "host:port",
    "save_path": "string (directory path; empty = auto-detect from /models)",
    "min_save_tokens": "int >= 0",
    "n_max_files": "int >= 0 (0 = unlimited)",
    "max_gb": "float >= 0 (0 = unlimited)",
    "thrash_window": "int >= 2",
    "thrash_max_switches": "int >= 0",
    "tail_match_min": "int >= 1",
    "cache_reuse": "int >= 0 (0 = off)",
    "cache_reuse_mode": "targeted | always",
    "health_poll_ms": "int > 0",
    "control_timeout_ms": "int > 0",
    "api_key": "string",
}


def default_config() -> dict[str, Any]:
    """A fresh copy of the built-in defaults."""
    return copy.deepcopy(DEFAULTS)


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override onto base (override wins); dicts merge recursively."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def resolve_config_path(flag_path: str | Path | None) -> Path:
    """Config location: explicit flag, else <project root>/config.json."""
    if flag_path:
        return Path(flag_path).expanduser()
    return PROJECT_ROOT / CONFIG_FILENAME


def load_config(flag_path: str | Path | None = None) -> tuple[dict[str, Any], Path]:
    """Load config; auto-generate with defaults if missing.

    Returns (config, path).
    """
    path = resolve_config_path(flag_path)
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return _deep_merge(default_config(), loaded), path
    config = default_config()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_config(config, path)
    return config, path


def write_config(config: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def generate_config_text() -> str:
    """All keys with defaults + descriptions (--generate-config)."""
    lines = ["# proxycache config — all keys with defaults", ""]
    for key, description in DESCRIPTIONS.items():
        lines.append(f"# {key}: {description}")
    lines.append("")
    lines.append(json.dumps(DEFAULTS, indent=2, ensure_ascii=False))
    return "\n".join(lines)


def detect_save_paths(models_response: dict) -> dict[str, str]:
    """Auto-detect save_path per model from the /models endpoint.

    Parses each model's status.args for the --slot-save-path <dir> pair.
    Returns a model -> save_path mapping.

    (design: config — Auto-detection)
    """
    save_paths: dict[str, str] = {}
    for model in models_response.get("data", []):
        model_id = model.get("id", "")
        args = model.get("status", {}).get("args", [])
        # Find the --slot-save-path <dir> pair.
        for i in range(len(args) - 1):
            if args[i] == "--slot-save-path":
                save_paths[model_id] = args[i + 1]
                break
    return save_paths


def resolve_save_path(
    model: str,
    config_save_path: str,
    detected: dict[str, str],
) -> str | None:
    """Resolve the save_path for a model.

    Precedence:
    1. config_save_path (if non-empty) -> global override (all models).
    2. detected[model] (if present) -> per-model auto-detection.
    3. None -> no save_path available (save skipped; conv stays RAM-only).

    (design: config — Resolution precedence)
    """
    if config_save_path:
        return config_save_path
    return detected.get(model)
    return "\n".join(lines) + "\n"
