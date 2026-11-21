"""End-to-end tests for wired evaluate() + reward() pipeline in equiv/reward.py."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from equiv.extract import _check_eligibility
from equiv.reward import check_near_identical, evaluate, reward


def _task(head: str, definition: str, fname: str, *, unwind: int = 10, input_ranges: dict | None = None):
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
    elig.task.unwind = unwind
    elig.task.input_ranges = input_ranges
    return elig.task


def test_evaluate_passes_task_unwind_and_measures_real_speedup():
    """task.unwind=34 is passed to verify(), and popcount earns positive reward."""
    orig = "int popcnt(unsigned int x) { int c = 0; for (int i = 0; i < 32; i++) if (x & (1u << i)) c++; return c; }"
    cand = "int popcnt(unsigned int x) { int c = 0; while (x) { x &= x - 1; c++; } return c; }"
    task = _task("int popcnt(unsigned intx)", orig, "popcnt", unwind=34)

    res = evaluate(task, cand, skip_tests=True)
    assert res.status == "proven", res.details
    assert res.near_identical is False
    assert res.speedup > 1.2, f"Expected speedup > 1.2, got {res.speedup:.2f}x"
    assert reward(res) > 0.0


def test_evaluate_rejects_overlap_exploit_xor_swap():
    """XOR-swap is equivalent on separate buffers but broken when a == b (overlap check catches it)."""
    orig = "void swap_vals(int *a, int *b) { int t = *a; *a = *b; *b = t; }"
    cand = "void swap_vals(int *a, int *b) { *a ^= *b; *b ^= *a; *a ^= *b; }"
    task = _task("void swap_vals(int *a, int *b)", orig, "swap_vals")

    res = evaluate(task, cand, skip_tests=True)
    assert res.overlap_check == "failed", f"Expected overlap_check='failed', got {res.overlap_check}"
    assert res.status == "not_equivalent"
    assert reward(res) == 0.0


def test_evaluate_detects_near_identical_rename_and_cosmetic_rewrite():
    """Variable renaming and cosmetic rewrites undone by -O3 both get near_identical=True, reward=0."""
    orig = "int add_one(int x) { int y = x; y = y + 1; return y; }"
    renamed = "int add_one(int a) { int b = a; b = b + 1; return b; }"
    cosmetic = "int add_one(int x) { int a = x; int b = a + 1; return b; }"
    task = _task("int add_one(intx)", orig, "add_one", input_ranges={"_narrowed": True, "x": [-1000, 1000]})

    assert check_near_identical(task, renamed) is True
    assert check_near_identical(task, cosmetic) is True

    res = evaluate(task, cosmetic, skip_tests=True)
    assert res.status == "proven", res.details
    assert res.near_identical is True
    # Even with a positive base reward, near_identical must earn 0.0
    assert reward(res, base=0.1) == 0.0


def test_evaluate_rejects_bound_overfitting_exploit_via_large_input_checks():
    """Candidate `if (n > 8) return 0;` passes CBMC (MAXN=8) but fails the n > MAXN checks."""
    orig = "int sum_arr(int *a, int n) { int s = 0; for (int i = 0; i < n; i++) s += a[i]; return s; }"
    exploit = (
        "int sum_arr(int *a, int n) {\n"
        "  if (n > 8) return 0;\n"
        "  int s = 0; for (int i = 0; i < n; i++) s += a[i]; return s;\n"
        "}"
    )
    task = _task(
        "int sum_arr(int *a, intn)", orig, "sum_arr",
        input_ranges={"_narrowed": True, "_buf_range": [-100, 100]},
    )

    res = evaluate(task, exploit, skip_tests=True)
    assert res.status == "bound_overfit", f"Bound-overfitting exploit must be rejected, got {res}"
    assert res.large_input_check == "failed"
    assert reward(res) == 0.0


def test_evaluate_rewards_genuine_algorithmic_speedup():
    """Redundant two-pass array sum -> single-pass earns proven + ~2x speedup + positive reward."""
    orig = (
        "int sum_twice(int *a, int n) {\n"
        "  int s1 = 0, s2 = 0;\n"
        "  for (int i = 0; i < n; i++) s1 += a[i];\n"
        "  for (int i = 0; i < n; i++) s2 += a[i];\n"
        "  return s1 + s2;\n"
        "}"
    )
    cand = (
        "int sum_twice(int *a, int n) {\n"
        "  int s = 0;\n"
        "  for (int i = 0; i < n; i++) s += a[i];\n"
        "  return s * 2;\n"
        "}"
    )
    task = _task(
        "int sum_twice(int *a, intn)", orig, "sum_twice",
        unwind=10, input_ranges={"_narrowed": True, "_buf_range": [-100, 100]},
    )

    res = evaluate(task, cand, skip_tests=True)
    assert res.status == "proven", res.details
    assert res.near_identical is False
    assert res.speedup > 1.3, f"Expected speedup > 1.3x, got {res.speedup:.2f}x"
    assert reward(res) > 0.3
