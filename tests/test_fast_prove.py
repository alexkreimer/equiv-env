"""Fast-prove: var*var abstraction + candidate safety run (equiv/abstraction.py)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from equiv.abstraction import abstract_mul
from equiv.config import get_config
from equiv.verify import fast_prove_check, verify
from test_reward_pipeline import _task

DOT = "int dot(int *a, int *b, int n) { int s = 0; for (int i = 0; i < n; i++) s += a[i] * b[i]; return s; }"
DOT_REV = "int dot(int *a, int *b, int n) { int s = 0; for (int i = n - 1; i >= 0; i--) s += a[i] * b[i]; return s; }"
DOT_SWAPPED = "int dot(int *a, int *b, int n) { int s = 0; for (int i = 0; i < n; i++) s += b[i] * a[i]; return s; }"
DOT_WRONG = "int dot(int *a, int *b, int n) { int s = 0; for (int i = 0; i < n; i++) s += a[i] * a[i]; return s; }"
# Equal to DOT under wrap-around arithmetic, but signed overflow (UB) in real C.
DOT_OVERFLOW = ("int dot(int *a, int *b, int n) { int s = 0; for (int i = 0; i < n; i++)"
                " s += (a[i] * b[i] + 2147483000) - 2147483000; return s; }")


def _dot_task():
    return _task("int dot(int *a, int *b, int n)", DOT, "dot",
                 input_ranges={"_narrowed": True, "_buf_range": [-1000, 1000]})


def _fp(cand, **kw):
    return fast_prove_check(_dot_task(), cand, maxn=4, unwind=6, timeout_s=60, **kw)


# --- the rewrite -------------------------------------------------------------------

def test_abstract_mul_rewrites_only_variable_products():
    t = _dot_task()
    src, types = abstract_mul(t, "int dot(int *a, int *b, int n) { return a[0] * b[0] + 3 * n + n * 2; }")
    assert types == {"int"}
    assert "__absmul_int((a[0]), (b[0]))" in src
    assert "3 * n" in src and "n * 2" in src


def test_abstract_mul_handles_nesting_and_compound_assign():
    t = _dot_task()
    src, _ = abstract_mul(t, "int dot(int *a, int *b, int n) { int s = 1; s *= a[0]; return a[0] * (b[0] * n) + s; }")
    assert "s = __absmul_int((s), (a[0]))" in src
    assert "__absmul_int((a[0]), ((__absmul_int((b[0]), (n)))))" in src


def test_abstract_mul_gives_up_on_unsafe_compound_assign():
    t = _dot_task()
    assert abstract_mul(t, "int dot(int *a, int *b, int n) { a[0] *= b[0]; return a[0]; }") is None


# --- soundness and power -------------------------------------------------------------

def test_fast_prove_proves_reordered_dot_product():
    ok, detail, _ = _fp(DOT_REV)
    assert ok, detail


def test_fast_prove_knows_multiplication_commutes():
    ok, detail, _ = _fp(DOT_SWAPPED)
    assert ok, detail


def test_fast_prove_rejects_different_products():
    ok, detail, _ = _fp(DOT_WRONG)
    assert not ok and "abstract equivalence" in detail


def test_fast_prove_rejects_candidate_that_overflows():
    """Wrap-around equal but UB in C: the safety run must catch it."""
    ok, detail, _ = _fp(DOT_OVERFLOW)
    assert not ok and "candidate safety" in detail, detail


def test_fast_prove_overlap_variant_works():
    ok, detail, _ = _fp(DOT_SWAPPED, overlap=True)  # reordered + overlap takes ~60 s: too slow
    assert ok, detail


# --- integration with verify() -----------------------------------------------------

@pytest.fixture
def short_exact_timeout(monkeypatch):
    cfg = get_config()
    monkeypatch.setitem(cfg["cbmc"], "exact_timeout_s", 3)
    monkeypatch.setitem(cfg["cbmc"], "fast_prove", True)


def test_verify_falls_back_to_fast_prove_on_timeout(short_exact_timeout):
    r = verify(_dot_task(), DOT_REV, timeout_s=60, maxn=4, unwind=6)
    assert r.status == "proven", r.details
    assert r.details.startswith("fast-prove")


def test_verify_never_proves_overflowing_candidate(short_exact_timeout):
    r = verify(_dot_task(), DOT_OVERFLOW, timeout_s=60, maxn=4, unwind=6)
    assert r.status != "proven", r


def test_verify_still_refutes_wrong_candidate_exactly(short_exact_timeout):
    r = verify(_dot_task(), DOT_WRONG, timeout_s=60, maxn=4, unwind=6)
    assert r.status in ("not_equivalent", "unknown_timeout"), r
    assert r.status != "proven"


# --- overlap harness unwind bug (Decision 16) -------------------------------------------

COPY = "void cp(int *d, int *s, int n) { for (int i = 0; i < n; i++) d[i] = s[i]; }"


@pytest.mark.parametrize("maxn", [4, 8])
def test_overlap_check_accepts_correct_two_pointer_rewrite(maxn):
    """Regression: the overlap harness's MAXN*2 init loop exceeded the unwind bound,
    so every candidate for a two-pointer task 'failed' the overlap check."""
    from equiv.reward import _run_overlap_check
    t = _task("void cp(int *d, int *s, int n)", COPY, "cp")
    same = "void cp(int *d, int *s, int n) { int i = 0; while (i < n) { d[i] = s[i]; i++; } }"
    assert _run_overlap_check(t, same, maxn=maxn, unwind=maxn + 2, timeout_s=60,
                              extra_cbmc_flags=None) == "passed"


def test_overlap_check_still_rejects_aliasing_bug():
    from equiv.reward import _run_overlap_check
    t = _task("void cp(int *d, int *s, int n)", COPY, "cp")
    backwards = "void cp(int *d, int *s, int n) { for (int i = n - 1; i >= 0; i--) d[i] = s[i]; }"
    assert _run_overlap_check(t, backwards, maxn=4, unwind=6, timeout_s=60,
                              extra_cbmc_flags=None) == "failed"
