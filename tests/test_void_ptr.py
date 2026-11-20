"""Focused tests for single-level void* buffer support."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from equiv.extract import _check_eligibility, REASON_UNSUPPORTED_TYPE
from equiv.harness import generate_harness, generate_overlap_harness
from equiv.verify import check_original_ub, self_verify, verify
from equiv.testgen import run_tests


def _rec(func_def: str, fname: str) -> dict:
    return {
        "text": {
            "func_head": "",
            "func_def": func_def,
            "fname": fname,
            "path": "user/repo/mem.c",
            "real_error": "",
            "angha_deps": "",
        }
    }


def test_void_ptr_memzero_ub_self_equiv_and_tests():
    """void * buffer with paired length passes extraction, UB, self-equiv, and cheap tests."""
    src = (
        "void my_memzero(void *s, size_t n) {\n"
        "  unsigned char *p = (unsigned char *)s;\n"
        "  for (size_t i = 0; i < n; i++) p[i] = 0;\n"
        "}"
    )
    elig = _check_eligibility(_rec(src, "my_memzero"), "train")
    assert elig.eligible, f"Expected eligible, got {elig.reason_code}"
    assert elig.task.pointer_rules == {"s": "paired"}

    h = generate_harness(elig.task, src, maxn=8)
    assert "unsigned char s_o[MAXN];" in h.harness_src

    ub_free, _ = check_original_ub(elig.task, timeout_s=30, maxn=8)
    assert ub_free

    res = self_verify(elig.task, timeout_s=30, maxn=8)
    assert res.status == "proven", f"Expected proven, got {res.status}: {res.details}"

    # Verify a broken candidate is caught by CBMC
    broken = (
        "void my_memzero(void *s, size_t n) {\n"
        "  unsigned char *p = (unsigned char *)s;\n"
        "  for (size_t i = 0; i + 1 < n; i++) p[i] = 0;\n"
        "}"
    )
    bad_res = verify(elig.task, broken, timeout_s=30, maxn=8)
    assert bad_res.status == "not_equivalent"

    # Cheap test driver compiles and passes on equivalent candidate
    t_res = run_tests(elig.task, src, maxn=8, n_random=10)
    assert t_res.passed, f"run_tests failed: {t_res.details}"


def test_void_ptr_return_and_overlap_harness():
    """void * return value and 2-pointer overlap harness work cleanly with byte buffers."""
    src = (
        "void *my_copy(void *dst, const void *src, size_t n) {\n"
        "  unsigned char *d = (unsigned char *)dst;\n"
        "  const unsigned char *s = (const unsigned char *)src;\n"
        "  for (size_t i = 0; i < n; i++) d[i] = s[i];\n"
        "  return dst;\n"
        "}"
    )
    elig = _check_eligibility(_rec(src, "my_copy"), "train")
    assert elig.eligible, f"Expected eligible, got {elig.reason_code}"

    ub_free, _ = check_original_ub(elig.task, timeout_s=30, maxn=8)
    assert ub_free

    res = self_verify(elig.task, timeout_s=30, maxn=8)
    assert res.status == "proven", f"Expected proven, got {res.status}: {res.details}"

    ov = generate_overlap_harness(elig.task, src, maxn=8)
    assert ov is not None
    assert "unsigned char shared_o[MAXN * 2];" in ov.harness_src


def test_double_void_ptr_still_rejected():
    """void ** remains rejected as unsupported_type."""
    src = "void f(void **p, size_t n) { (void)p; (void)n; }"
    elig = _check_eligibility(_rec(src, "f"), "train")
    assert not elig.eligible
    assert elig.reason_code == REASON_UNSUPPORTED_TYPE
