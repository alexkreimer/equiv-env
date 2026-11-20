"""Regression tests for dataset/harness bugfixes."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from equiv.extract import _check_eligibility, get_paired_length_params


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
