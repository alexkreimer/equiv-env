"""Regression tests for dataset/harness bugfixes."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from equiv.extract import _check_eligibility, get_paired_length_params
from equiv.harness import generate_harness, generate_overlap_harness, generate_ub_harness, _emit_range_assume
from equiv.verify import check_original_ub, self_verify


def _make_record(func_head: str, func_def: str, fname: str, path: str = "user/repo/a.c") -> dict:
    return {
        "text": {
            "func_head": func_head,
            "func_def": func_def,
            "fname": fname,
            "path": path,
            "real_error": "",
            "angha_deps": "",
        }
    }


def test_mangled_func_head_parsed_from_func_def():
    """Bug 1: ExeBench drops spaces in scalar params in func_head (`intn`, `intsize`)."""
    rec = _make_record(
        func_head="void initArray(int *a, intn)",
        func_def="void initArray(int *a, int n) {\n    for (int i = 0; i < n; i++) a[i] = i;\n}",
        fname="initArray",
    )
    elig = _check_eligibility(rec, "train")
    assert elig.eligible, f"Expected eligible, got {elig.reason_code}"
    assert elig.task.signature.params == [("a", "int *"), ("n", "int")]
    assert elig.task.pointer_rules == {"a": "paired"}
    assert get_paired_length_params(elig.task.signature, elig.task.pointer_rules) == {"n"}


def test_bare_unsigned_and_array_size_params():
    """Bug 1b: bare `unsigned` and fixed array syntax `int arr[4]` parse cleanly."""
    rec = _make_record(
        func_head="int sum4(int *arr, unsignedn)",
        func_def="int sum4(int arr[4], unsigned n) {\n    unsigned i; int s = 0;\n    for (i = 0; i < n; i++) s += arr[i];\n    return s;\n}",
        fname="sum4",
    )
    elig = _check_eligibility(rec, "train")
    assert elig.eligible, f"Expected eligible, got {elig.reason_code}"
    assert elig.task.signature.params == [("arr", "int *"), ("n", "unsigned")]
    assert elig.task.pointer_rules == {"arr": "paired"}


def test_ub_check_passes_for_string_loop():
    """Bug 2a: clean string loop `my_strlen(char *p)` must NOT fail ub_in_original."""
    rec = _make_record(
        func_head="int my_strlen(char *p)",
        func_def="int my_strlen(char *p) { int c = 0; while (*p != '\\0') { p++; c++; } return c; }",
        fname="my_strlen",
    )
    elig = _check_eligibility(rec, "train")
    assert elig.eligible
    assert elig.task.pointer_rules == {"p": "string"}

    ub_free, ranges = check_original_ub(elig.task, timeout_s=30, maxn=8)
    assert ub_free, "Clean string loop was falsely rejected as ub_in_original"

    res = self_verify(elig.task, timeout_s=30, maxn=8, extra_cbmc_flags=["--no-signed-overflow-check"])
    assert res.status == "proven", f"Expected proven, got {res.status}: {res.details}"


def test_ub_check_and_self_equiv_for_pointer_return_string_loop():
    """Bug 2a + pointer return: `char *to_unix_path(char *p)` passes UB + self-equiv."""
    rec = _make_record(
        func_head="char *to_unix_path(char *p)",
        func_def=(
            "char *to_unix_path(char *p) {\n"
            "  char *pp = p;\n"
            "  while (*pp != 0) { if (*pp == '\\\\') *pp = '/'; ++pp; }\n"
            "  return p;\n"
            "}"
        ),
        fname="to_unix_path",
    )
    elig = _check_eligibility(rec, "train")
    assert elig.eligible
    ub_free, _ = check_original_ub(elig.task, timeout_s=30, maxn=8)
    assert ub_free, "to_unix_path falsely rejected as ub_in_original"

    res = self_verify(elig.task, timeout_s=30, maxn=8, extra_cbmc_flags=["--no-signed-overflow-check"])
    assert res.status == "proven", f"Expected proven for pointer return, got {res.status}: {res.details}"


def test_paired_array_length_passes_ub_and_self_equiv():
    """Bug 2b & Bug 3: `sum_arr(int *a, int n)` gets `0 <= n <= MAXN` in both UB and main harness."""
    rec = _make_record(
        func_head="int sum_arr(int *a, intn)",
        func_def="int sum_arr(int *a, int n) { int s = 0; for (int i = 0; i < n; i++) s += a[i]; return s; }",
        fname="sum_arr",
    )
    elig = _check_eligibility(rec, "train")
    assert elig.eligible
    task = elig.task

    # Check harness explicitly constrains n to [0, MAXN]
    h = generate_harness(task, task.func_src, maxn=8)
    assert "__CPROVER_assume(n >= 0 && n <= MAXN);" in h.harness_src

    ub_h = generate_ub_harness(task, maxn=8)
    assert "__CPROVER_assume(n >= 0 && n <= MAXN);" in ub_h.harness_src

    # Original overflows full-range int32 sum of 8 elements, so narrowing to [-32768, 32767] rescues it
    ub_free, ranges = check_original_ub(task, timeout_s=30, maxn=8)
    assert ub_free, "sum_arr should pass UB check after narrowing"
    assert ranges and ranges.get("_narrowed") is True
    task.input_ranges = ranges

    # Main harness must apply narrowed buffer element ranges too
    h_narrow = generate_harness(task, task.func_src, maxn=8)
    assert "-32768" in h_narrow.harness_src and "32767" in h_narrow.harness_src

    res = self_verify(task, timeout_s=30, maxn=8)
    assert res.status == "proven", f"Expected proven with full safety checks, got {res.status}: {res.details}"


def test_unsigned_narrow_range_is_not_unsatisfiable():
    """Soundness guard: unsigned types must NEVER get `x >= -32768` (unsatisfiable in C)."""
    assume = _emit_range_assume("x", "unsigned int", -32768, 32767)
    assert assume == "__CPROVER_assume(x <= 32767ULL);"
    assert "-" not in assume

    assume_u8 = _emit_range_assume("c", "unsigned char", -32768, 32767)
    assert assume_u8 is None  # [0, 255] already inside [-32768, 32767]


def test_overlap_harness_includes_paired_and_string_rules():
    """Bug 3b: overlap harness applies paired length bounds and string terminators."""
    rec = _make_record(
        func_head="void copy_arr(int *dst, int *src, intn)",
        func_def="void copy_arr(int *dst, int *src, int n) { for (int i = 0; i < n; i++) dst[i] = src[i]; }",
        fname="copy_arr",
    )
    elig = _check_eligibility(rec, "train")
    assert elig.eligible
    ov = generate_overlap_harness(elig.task, elig.task.func_src, maxn=8)
    assert ov is not None
    assert "__CPROVER_assume(n >= 0 && n <= MAXN);" in ov.harness_src
