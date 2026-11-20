"""Tests for standard POSIX/BSD/Linux integer typedef support and stripping of
AnghaBench fake `/*<<< orphan*/` redefinitions."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from equiv.extract import _check_eligibility, REASON_UNSUPPORTED_TYPE
from equiv.verify import check_original_ub, self_verify, verify


def _rec(func_def: str, fname: str, deps: str = "") -> dict:
    return {
        "text": {
            "func_head": "",
            "func_def": func_def,
            "fname": fname,
            "path": "user/repo/types.c",
            "real_error": "",
            "angha_deps": deps,
        }
    }


def test_posix_bsd_linux_integer_typedefs_pass():
    """Functions using u_char, u_int, __u32, __u64, off_t, time_t pass extraction and CBMC."""
    src = (
        "__u32 sum_bytes(const u_char *buf, u_int n) {\n"
        "  __u32 s = 0;\n"
        "  for (u_int i = 0; i < n; i++) s += buf[i];\n"
        "  return s;\n"
        "}"
    )
    # Include fake AnghaBench orphan/wrong typedefs in deps to verify _filter_deps strips them
    deps = (
        "typedef int /*<<< orphan*/ u_char ;\n"
        "typedef char* __u64 ;\n"
        "typedef long scalar_t__ ;\n"
        "typedef scalar_t__ time_t ;\n"
    )
    elig = _check_eligibility(_rec(src, "sum_bytes", deps), "train")
    assert elig.eligible, f"Rejected: {elig.reason_code}"
    assert elig.task.pointer_rules == {"buf": "paired"}

    ub_free, _ = check_original_ub(elig.task, timeout_s=30, maxn=8)
    assert ub_free

    res = self_verify(elig.task, timeout_s=30, maxn=8)
    assert res.status == "proven"

    bad = (
        "__u32 sum_bytes(const u_char *buf, u_int n) {\n"
        "  return 0;\n"
        "}"
    )
    assert verify(elig.task, bad, timeout_s=30, maxn=8).status == "not_equivalent"


def test_fake_anghabench_u64_char_ptr_stripped():
    """AnghaBench's fake `typedef char* __u64;` must be stripped so __u64 works as 64-bit int."""
    src = "__u64 mask_low(__u64 x, off_t shift) { return x & ((1ULL << (shift & 7)) - 1); }"
    deps = "typedef char* __u64 ;\ntypedef int off_t ;\n"
    elig = _check_eligibility(_rec(src, "mask_low", deps), "train")
    assert elig.eligible

    ub_free, _ = check_original_ub(elig.task, timeout_s=30, maxn=8)
    assert ub_free

    res = self_verify(elig.task, timeout_s=30, maxn=8)
    assert res.status == "proven"


def test_opaque_orphan_typedefs_remain_rejected():
    """Opaque types with fake AnghaBench `typedef int /*<<< orphan*/ FILE;` stay rejected."""
    for bad_src, name in [
        ("int read_file(FILE *f, int n) { (void)f; return n; }", "read_file"),
        ("int simd_op(__m128 x) { (void)x; return 0; }", "simd_op"),
    ]:
        deps = "typedef int /*<<< orphan*/ FILE ;\ntypedef int /*<<< orphan*/ __m128 ;\n"
        elig = _check_eligibility(_rec(bad_src, name, deps), "train")
        assert not elig.eligible
        assert elig.reason_code == REASON_UNSUPPORTED_TYPE
