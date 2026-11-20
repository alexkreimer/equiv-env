"""Tests for equiv/speed.py — separate-TU in-process timing, checksum diff check, Spearman."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from equiv.extract import _check_eligibility
from equiv.speed import (
    BENCH_N,
    default_arch,
    is_emulated,
    machine_fingerprint,
    measure_speedup,
    spearman,
)


def _task(head: str, definition: str, fname: str):
    rec = {
        "text": {
            "func_head": head,
            "func_def": definition,
            "fname": fname,
            "path": "u/r/a.c",
            "real_error": "",
            "angha_deps": "",
        }
    }
    elig = _check_eligibility(rec, "train")
    assert elig.eligible, elig.reason_code
    return elig.task


def test_spearman_exact_and_ties():
    assert abs(spearman([1.0, 2.0, 3.0, 4.0], [10.0, 20.0, 30.0, 40.0]) - 1.0) < 1e-9
    assert abs(spearman([1.0, 2.0, 3.0, 4.0], [40.0, 30.0, 20.0, 10.0]) - (-1.0)) < 1e-9
    # Constant vector -> 0.0
    assert spearman([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) == 0.0
    # Ties handled cleanly
    r = spearman([1.0, 1.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0])
    assert 0.9 < r < 1.0


def test_machine_fingerprint_recorded():
    fp = machine_fingerprint()
    assert fp["machine"]
    assert fp["system"]
    assert fp["bench_n"] == BENCH_N
    assert not is_emulated()


def test_inproc_detects_real_loop_to_formula_speedup():
    """O(n^2) / O(n) loop vs O(1) closed form must show a clear speedup and matching checksum."""
    orig = (
        "int sum_to_n(int n) {\n"
        "  int s = 0;\n"
        "  for (int i = 1; i <= n; i++) s += i;\n"
        "  return s;\n"
        "}"
    )
    cand = "int sum_to_n(int n) { return n * (n + 1) / 2; }"
    task = _task("int sum_to_n(intn)", orig, "sum_to_n")
    r = measure_speedup(task, cand, proxy="inproc_time")
    assert r.error is None, r.error
    assert r.outputs_match is True
    assert r.orig_value is not None and r.cand_value is not None
    # Separate TU + varying args prevents constant folding; O(1) beats loop
    assert r.speedup > 1.05, f"Expected speedup > 1.05, got {r.speedup:.2f}x ({r.orig_value:.2f} -> {r.cand_value:.2f} ns)"


def test_inproc_detects_large_buffer_algorithmic_speedup():
    """O(n^2) duplicate scan on BENCH_N=512 buffer vs O(n) single-pass sum."""
    orig = (
        "int prefix_total(int *a, int n) {\n"
        "  int s = 0;\n"
        "  for (int i = 0; i < n; i++) {\n"
        "    for (int j = 0; j <= i; j++) s += a[j];\n"
        "  }\n"
        "  return s;\n"
        "}"
    )
    cand = (
        "int prefix_total(int *a, int n) {\n"
        "  int s = 0, run = 0;\n"
        "  for (int i = 0; i < n; i++) { run += a[i]; s += run; }\n"
        "  return s;\n"
        "}"
    )
    task = _task("int prefix_total(int *a, intn)", orig, "prefix_total")
    r = measure_speedup(task, cand, proxy="inproc_time")
    assert r.error is None, r.error
    assert r.outputs_match is True
    assert r.speedup > 5.0, f"Expected large O(n^2)->O(n) speedup, got {r.speedup:.2f}x"


def test_large_input_checksum_catches_bound_overfitting_exploit():
    """Candidate that returns 0 for n > 8 (CBMC MAXN) must fail outputs_match on BENCH_N=512."""
    orig = "int sum_arr(int *a, int n) { int s = 0; for (int i = 0; i < n; i++) s += a[i]; return s; }"
    exploit = (
        "int sum_arr(int *a, int n) {\n"
        "  if (n > 8) return 0;\n"
        "  int s = 0; for (int i = 0; i < n; i++) s += a[i]; return s;\n"
        "}"
    )
    task = _task("int sum_arr(int *a, intn)", orig, "sum_arr")
    r = measure_speedup(task, exploit, proxy="inproc_time")
    assert r.error is None, r.error
    assert r.outputs_match is False, "Bound-hacking exploit (if n > 8 return 0) must fail large-input checksum!"


def test_static_asm_proxy_works():
    orig = "int popcnt(unsigned int x) { int c = 0; for (int i = 0; i < 32; i++) if (x & (1u << i)) c++; return c; }"
    cand = "int popcnt(unsigned int x) { int c = 0; while (x) { x &= x - 1; c++; } return c; }"
    task = _task("int popcnt(unsigned intx)", orig, "popcnt")
    r = measure_speedup(task, cand, proxy="static_asm")
    assert r.error is None, r.error
    assert r.orig_value and r.cand_value
    assert r.speedup > 0.0
