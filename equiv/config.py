"""
config.py — Load central config.yaml and compute deterministic config hashes.

Usage:
    from equiv.config import load_config, get_config, config_hash
    cfg = get_config()
    h = config_hash(cfg)
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

DEFAULT_CONFIG: dict[str, Any] = {
    "cbmc": {
        "arch": "x86_64",
        "maxn": 4,                # proof bound for candidates; n > maxn is covered by large_tests
        "unwind_candidates": [10, 34, 66],
        "timeout_s": 60,
        "fast_prove": True,       # on exact-check timeout, try abstraction-based proof
        "exact_timeout_s": 20,    # exact-check budget when fast_prove is on
        "ub_timeout_s": 30,
        "memory_limit_gb": 8,
    },
    "dataset": {
        "allowed_libc": [
            "memset", "memcpy", "memmove",
            "strlen", "strcmp", "strncmp", "strcpy", "strchr",
            "abs", "labs", "llabs",
        ],
        "excluded_path_patterns": [
            "gcc/testsuite",
            "llvm/test",
            "clang/test",
            "tigress",
            "obfuscated-programs",
            "csmith",
        ],
        "max_per_file": 10,
        "max_per_repo": 200,
        "deduplicate": True,
    },
    "tests": {
        "n_random": 200,
        "n_heavy": 2000,   # in-domain random tests, analysis mode only
    },
    "large_tests": {       # n > MAXN differential tests (Stage 0.5 P4)
        "n_random": 64,
        "bench_n": 512,
        "seed": 0,
    },
    "speed": {
        "proxy": "callgrind",
        "arch": "armv8.5-a",
    },
    "reward": {
        "cap": 4.0,
        "min_gain": 1.05,
        "base": 0.0,
    },
    "cache": {
        "path": "data/eval_cache.db",
    },
}

_cached_default: dict[str, Any] | None = None


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into a copy of base."""
    merged = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = copy.deepcopy(v)
    return merged


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load configuration from YAML, merged over DEFAULT_CONFIG.

    If path is None, checks EQUIV_CONFIG env var, then repo-root config.yaml.
    """
    if path is None:
        env_path = os.environ.get("EQUIV_CONFIG")
        cfg_path = Path(env_path) if env_path else DEFAULT_CONFIG_PATH
    else:
        cfg_path = Path(path)

    if not cfg_path.exists():
        if path is not None:
            raise FileNotFoundError(f"Config file not found: {cfg_path}")
        return copy.deepcopy(DEFAULT_CONFIG)

    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    if not isinstance(raw, dict):
        raise ValueError(f"Config file must contain a YAML mapping: {cfg_path}")

    return _deep_merge(DEFAULT_CONFIG, raw)


def get_config(reload: bool = False) -> dict[str, Any]:
    """Return cached default config (loaded from EQUIV_CONFIG or config.yaml)."""
    global _cached_default
    if _cached_default is None or reload:
        _cached_default = load_config()
    return _cached_default


def config_hash(config: dict[str, Any] | None = None) -> str:
    """Compute a deterministic 16-char SHA-256 hex digest for a config dict."""
    cfg = get_config() if config is None else config
    canonical = json.dumps(cfg, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
