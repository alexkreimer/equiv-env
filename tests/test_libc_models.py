"""Verification tests for newly allow-listed libc models (abs, labs, llabs,
memmove, strcmp, strncmp, strcpy, strchr) with _FORTIFY_SOURCE=0."""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from equiv.extract import _check_eligibility
from equiv.verify import check_original_ub, self_verify, verify


def _task(func_def: str, fname: str):
    rec = {
        "text": {
            "func_head": "",
            "func_def": func_def,
            "fname": fname,
            "path": "user/repo/libc_use.c",
            "real_error": "",
            "angha_deps": "",
        }
    }
    elig = _check_eligibility(rec, "train")
    assert elig.eligible, f"{fname} rejected: {elig.reason_code}"
    return elig.task


@pytest.mark.parametrize(
    "name,orig,cand_good,cand_bad",
    [
        (
            "abs_val",
            "int abs_val(int x) { return x < 0 ? -x : x; }",
            "int abs_val(int x) { return abs(x); }",
            "int abs_val(int x) { return abs(x) + 1; }",
        ),
        (
            "labs_val",
            "long labs_val(long x) { return x < 0 ? -x : x; }",
            "long labs_val(long x) { return labs(x); }",
            "long labs_val(long x) { return labs(x) ^ 1; }",
        ),
        (
            "llabs_val",
            "long long llabs_val(long long x) { return x < 0 ? -x : x; }",
            "long long llabs_val(long long x) { return llabs(x); }",
            "long long llabs_val(long long x) { return llabs(x) - 1; }",
        ),
        (
            "shift_memmove",
            "void shift_memmove(int *arr, int n) { if (n > 1) memmove(arr + 1, arr, (size_t)(n - 1) * sizeof(int)); }",
            "void shift_memmove(int *arr, int n) { if (n >= 2) memmove(arr + 1, arr, (size_t)(n - 1) * sizeof(int)); }",
            "void shift_memmove(int *arr, int n) { if (n > 2) memmove(arr + 1, arr, (size_t)(n - 2) * sizeof(int)); }",
        ),
        (
            "my_strcpy",
            "void my_strcpy(char *dst, const char *src) { while ((*dst++ = *src++) != 0); }",
            "void my_strcpy(char *dst, const char *src) { strcpy(dst, src); }",
            "void my_strcpy(char *dst, const char *src) { dst[0] = src[0]; }",
        ),
        (
            "my_strchr",
            "char *my_strchr(const char *s, int c) { for (; *s != (char)c; ++s) if (*s == 0) return NULL; return (char *)s; }",
            "char *my_strchr(const char *s, int c) { return strchr(s, c); }",
            "char *my_strchr(const char *s, int c) { return *s == (char)c ? (char *)s : NULL; }",
        ),
        (
            "str_eq",
            "int str_eq(const char *a, const char *b) { while (*a && *a == *b) { a++; b++; } return *a == *b; }",
            "int str_eq(const char *a, const char *b) { return strcmp(a, b) == 0; }",
            "int str_eq(const char *a, const char *b) { return a[0] == b[0]; }",
        ),
        (
            "strn_eq",
            "int strn_eq(const char *a, const char *b, size_t n) { for (size_t i = 0; i < n; i++) { if (a[i] != b[i]) return 0; if (a[i] == 0) return 1; } return 1; }",
            "int strn_eq(const char *a, const char *b, size_t n) { return strncmp(a, b, n) == 0; }",
            "int strn_eq(const char *a, const char *b, size_t n) { return n == 0 ? 1 : a[0] == b[0]; }",
        ),
    ],
)
def test_libc_model_proven_and_refuted(name, orig, cand_good, cand_bad):
    """Each allow-listed libc function must prove valid equivalence and refute wrong code."""
    task = _task(orig, name)
    good = verify(task, cand_good, timeout_s=30, maxn=8)
    assert good.status == "proven", f"{name} good candidate failed: {good.status} ({good.details})"

    bad = verify(task, cand_bad, timeout_s=30, maxn=8)
    assert bad.status == "not_equivalent", f"{name} bad candidate not caught: {bad.status}"


def test_real_exebench_nqueens_check_diagonal_with_abs():
    """Real ExeBench N-Queens nested loop calling abs() passes extraction, UB, and self-equiv."""
    src = (
        "int check_diagonal(int *list, int n) {\n"
        "  for (int i = 0; i < n; i++) {\n"
        "    for (int j = 0; j < n; j++) {\n"
        "      if (i == j) continue;\n"
        "      int colDiff = abs((j + 1) - (i + 1));\n"
        "      int rowDiff = abs(list[j] - list[i]);\n"
        "      if (colDiff == rowDiff) return 0;\n"
        "    }\n"
        "  }\n"
        "  return 1;\n"
        "}"
    )
    task = _task(src, "check_diagonal")
    ub_free, ranges = check_original_ub(task, timeout_s=30, maxn=8)
    assert ub_free
    if ranges:
        task.input_ranges = ranges
    res = self_verify(task, timeout_s=30, maxn=8)
    assert res.status == "proven", f"Expected proven, got {res.status}: {res.details}"
