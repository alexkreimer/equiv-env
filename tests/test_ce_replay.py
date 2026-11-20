"""Counterexample replay: CBMC trace parsing + replay on compiled code."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from equiv.extract import _check_eligibility
from equiv.replay import replay_counterexample
from equiv.verify import _parse_trace, verify


def _task(head: str, definition: str, fname: str):
    rec = {"text": {"func_head": head, "func_def": definition, "fname": fname,
                    "path": "u/r/a.c", "real_error": "", "angha_deps": ""}}
    elig = _check_eligibility(rec, "train")
    assert elig.eligible, elig.reason_code
    return elig.task


def test_parse_trace_captures_buffers_and_kind():
    trace = """Trace for harness.assertion.1:

State 14 file x.c function harness line 68 thread 0
----------------------------------------------------
  n=3 (00000000 00000000 00000000 00000011)

State 16 file x.c function harness line 73 thread 0
----------------------------------------------------
  a_o={ 0, 0, 0, 0 } ({ 00000000, 00000000, 00000000, 00000000 })

State 23 file x.c function harness line 76 thread 0
----------------------------------------------------
  a_o[1l]=-7 (11111111 11111111 11111111 11111001)

State 24 file x.c function harness line 77 thread 0
----------------------------------------------------
  a_c[1l]=-7 (11111111 11111111 11111111 11111001)

State 30 file x.c function orig__f line 5 thread 0
----------------------------------------------------
  a_o[1l]=99 (00000000)

Violated property:
  file x.c line 90 function harness
"""
    ce = _parse_trace(trace)
    assert ce["inputs"] == {"n": "3"}
    # initial contents only: the write inside orig__f must not leak in
    assert ce["buffers"] == {"a": [0, -7, 0, 0]}
    assert ce["kind"] == "mismatch"


def test_mismatch_counterexample_with_buffer_replays():
    """The bug: buffer contents were never captured, so replay ran on zeros and failed."""
    orig = "int count_pos(int *a, int n) { int c = 0; for (int i = 0; i < n; i++) if (a[i] > 0) c++; return c; }"
    cand = "int count_pos(int *a, int n) { int c = 0; for (int i = 0; i < n; i++) if (a[i] >= 0) c++; return c; }"
    task = _task("int count_pos(int *a, intn)", orig, "count_pos")
    vr = verify(task, cand, timeout_s=60)
    assert vr.status == "not_equivalent"
    assert vr.counterexample["kind"] == "mismatch"
    assert vr.counterexample["buffers"]["a"], "buffer contents must be captured"
    r = replay_counterexample(task, cand, vr.counterexample)
    assert r.status == "replayed", r.details


def test_scalar_mismatch_replays():
    orig = "int avg(int a, int b) { return a + (b - a) / 2; }"
    cand = "int avg(int a, int b) { return (a + b) / 2; }"
    task = _task("int avg(inta, intb)", orig, "avg")
    vr = verify(task, cand, timeout_s=60)
    assert vr.status == "not_equivalent"
    assert replay_counterexample(task, cand, vr.counterexample).status == "replayed"


def test_candidate_ub_counterexample_replays_under_sanitizers():
    orig = "int last(int *a, int n) { return n > 0 ? a[n-1] : 0; }"
    cand = "int last(int *a, int n) { return a[n-1]; }"
    task = _task("int last(int *a, intn)", orig, "last")
    vr = verify(task, cand, timeout_s=60)
    assert vr.status == "not_equivalent"
    assert vr.counterexample["kind"] == "ub"
    r = replay_counterexample(task, cand, vr.counterexample)
    assert r.status == "replayed", r.details


def test_equal_functions_are_not_reproduced():
    """A bogus counterexample must NOT be reported as replayed."""
    src = "int f(int a) { return a + 1; }"
    task = _task("int f(inta)", src, "f")
    r = replay_counterexample(task, src, {"inputs": {"a": "5"}, "buffers": {}, "kind": "mismatch"})
    assert r.status == "not_reproduced"


def test_uncompilable_candidate_is_driver_error_not_a_checker_bug():
    task = _task("int f(inta)", "int f(int a) { return a + 1; }", "f")
    r = replay_counterexample(task, "int f(int a) { return a +; }",
                              {"inputs": {"a": "5"}, "buffers": {}, "kind": "mismatch"})
    assert r.status == "driver_error"


def test_pointer_return_compared_by_offset_not_address():
    orig = "char *skip(char *p) { while (*p == ' ') p++; return p; }"
    task = _task("char *skip(char *p)", orig, "skip")
    ce = {"inputs": {}, "buffers": {"p": [32, 32, 65, 0]}, "kind": "mismatch"}
    # identical function: distinct stack buffers must not look "different"
    assert replay_counterexample(task, orig, ce).status == "not_reproduced"
    # genuinely different offset must be detected
    cand = "char *skip(char *p) { return p; }"
    assert replay_counterexample(task, cand, ce).status == "replayed"


def test_parse_trace_preserves_space_char_literal():
    """CBMC prints space char as `' '` — splitting on whitespace used to corrupt it to 0."""
    trace = """Trace for harness.assertion.1:

State 16 file x.c function harness line 73 thread 0
----------------------------------------------------
  str_o={ 0, 0, 0, 0 } ({ 00000000, 00000000, 00000000, 00000000 })

State 23 file x.c function harness line 76 thread 0
----------------------------------------------------
  str_o[0l]=' ' (00100000)

Violated property:
  file x.c line 90 function harness
"""
    ce = _parse_trace(trace)
    assert ce["buffers"]["str"] == [32, 0, 0, 0]


def test_inline_function_replays_without_linker_error():
    """C99 `inline` without `static` emits no symbol at -O0 unless stripped."""
    orig = "inline int sign(int x) { return x < 0 ? -1 : 1; }"
    cand = "inline int sign(int x) { return x <= 0 ? -1 : 1; }"
    task = _task("inline int sign(intx)", orig, "sign")
    vr = verify(task, cand, timeout_s=30)
    assert vr.status == "not_equivalent"
    r = replay_counterexample(task, cand, vr.counterexample)
    assert r.status == "replayed", r.details


def test_static_string_return_without_buffer_params_replays():
    orig = 'static const char *name(int c) { if (c == 1) return "one"; return "other"; }'
    cand = 'static const char *name(int c) { if (c == 2) return "one"; return "other"; }'
    task = _task("static const char *name(intc)", orig, "name")
    vr = verify(task, cand, timeout_s=30)
    assert vr.status == "not_equivalent"
    r = replay_counterexample(task, cand, vr.counterexample)
    assert r.status == "replayed", r.details


def test_missing_return_mutant_replays_as_ub():
    orig = "int abs_v(int x) { return x < 0 ? -x : x; }"
    cand = "int abs_v(int x) { }"
    task = _task("int abs_v(intx)", orig, "abs_v")
    ce = {"inputs": {"x": "1"}, "buffers": {}, "kind": "mismatch"}
    r = replay_counterexample(task, cand, ce)
    assert r.status == "replayed", r.details
