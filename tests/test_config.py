"""Tests for equiv.config and dataset source exclusion rules."""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from equiv.config import config_hash, get_config, load_config
from equiv.extract import REASON_EXCLUDED_SOURCE, _check_eligibility
from equiv.reward import EvalResult, init as reward_init, reward


def test_load_default_config():
    cfg = load_config()
    assert cfg["cbmc"]["maxn"] == 8
    assert cfg["cbmc"]["arch"] == "x86_64"
    assert "gcc/testsuite" in cfg["dataset"]["excluded_path_patterns"]
    assert "tigress" in cfg["dataset"]["excluded_path_patterns"]
    assert cfg["dataset"]["max_per_file"] == 10
    assert cfg["reward"]["cap"] == 4.0
    assert cfg["reward"]["min_gain"] == 1.05
    assert cfg["reward"]["base"] == 0.0


def test_config_hash_deterministic_and_sensitive():
    cfg1 = load_config()
    cfg2 = load_config()
    assert config_hash(cfg1) == config_hash(cfg2)
    assert len(config_hash(cfg1)) == 16

    cfg2["cbmc"]["maxn"] = 6
    assert config_hash(cfg1) != config_hash(cfg2)


def test_load_custom_partial_config(tmp_path: Path):
    custom = tmp_path / "custom.yaml"
    custom.write_text("reward:\n  min_gain: 1.10\n", encoding="utf-8")
    cfg = load_config(custom)
    # Overridden value
    assert cfg["reward"]["min_gain"] == 1.10
    # Inherited defaults
    assert cfg["reward"]["cap"] == 4.0
    assert cfg["cbmc"]["maxn"] == 8


def test_excluded_source_patterns():
    base_rec = {
        "func_def": "int f(int *a) { return a[0] + 1; }",
        "func_head": "int f(int *a)",
        "fname": "f",
        "real_error": "",
        "angha_deps": "",
    }

    gcc_rec = dict(base_rec, path="BreakawayConsulting/gcc/gcc/testsuite/gcc.target/sh/pr49263.c")
    res_gcc = _check_eligibility(gcc_rec, "train")
    assert not res_gcc.eligible
    assert res_gcc.reason_code == REASON_EXCLUDED_SOURCE

    tigress_rec = dict(base_rec, path="tum-i22/obfuscated-programs/tigress-generated-programs/foo.c")
    res_tig = _check_eligibility(tigress_rec, "train")
    assert not res_tig.eligible
    assert res_tig.reason_code == REASON_EXCLUDED_SOURCE

    real_rec = dict(base_rec, path="someuser/myproj/src/math.c")
    res_real = _check_eligibility(real_rec, "train")
    assert res_real.eligible

    # Explicit empty override disables exclusion
    res_override = _check_eligibility(gcc_rec, "train", excluded_path_patterns=[])
    assert res_override.eligible


def test_reward_uses_config_defaults(tmp_path: Path):
    cfg = get_config()
    reward_init(cfg, cache_path=str(tmp_path / "cache.db"))

    r_fast = EvalResult(status="proven", task_id="t1", speedup=2.0)
    assert reward(r_fast) == 1.0

    r_slow = EvalResult(status="proven", task_id="t1", speedup=1.02)
    assert reward(r_slow) == 0.0  # below min_gain=1.05, base=0.0

    # Explicit override still works
    assert reward(r_slow, min_gain=1.01) == 1.02 - 1.0
