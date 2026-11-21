"""Stage 0.5 pre-work (P1–P5): large-input checks, new statuses, analysis mode, escape rates."""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from equiv import testgen
from equiv.reward import evaluate, reward
from test_reward_pipeline import _task


@pytest.fixture(autouse=True)
def _no_eval_cache(monkeypatch):
    """Other tests may call reward.init(); a shared cache would leak results between
    these tests (the cache key does not include monkeypatched behavior)."""
    monkeypatch.setattr(sys.modules["equiv.reward"], "_cache", None)

SUM_ORIG = "int sum_arr(int *a, int n) { int s = 0; for (int i = 0; i < n; i++) s += a[i]; return s; }"
# Correct for n <= MAXN = 8; wrong for some (not all) n > 8. The fixed BENCH_N = 512
# checksum misses it (512 % 7 == 1); random lengths in (8, 512] do not.
SUM_SOMETIMES_WRONG = (
    "int sum_arr(int *a, int n) { int s = 0; for (int i = 0; i < n; i++) s += a[i];"
    " if (n > 8 && n % 7 == 3) s += 1; return s; }"
)
# Correct rewrite in the same summation order. Reordered sums (reversed, unrolled with two
# accumulators) are correct too but CBMC times out on them at 60 s (SAT on reassociated adds).
SUM_SAME_ORDER = "int sum_arr(int *a, int n) { int s = 0, i = 0; while (i < n) s += a[i++]; return s; }"


def _sum_task():
    return _task("int sum_arr(int *a, int n)", SUM_ORIG, "sum_arr",
                 input_ranges={"_narrowed": True, "_buf_range": [-100, 100]})


# --- P4: large-input random tests ------------------------------------------------

def test_large_tests_catch_candidate_wrong_only_for_some_large_n():
    task = _sum_task()
    assert testgen.run_tests(task, SUM_SOMETIMES_WRONG).passed  # cheap tests are blind to it
    assert testgen.run_large_tests(task, SUM_SOMETIMES_WRONG).verdict == "failed"
    assert testgen.run_large_tests(task, SUM_SAME_ORDER).verdict == "passed"


def test_fixed_checksum_alone_misses_it_but_evaluate_does_not():
    """Golden case for P4: proven by CBMC, passes the BENCH_N checksum, still bound_overfit."""
    from equiv.speed import measure_speedup

    task = _sum_task()
    assert measure_speedup(task, SUM_SOMETIMES_WRONG, proxy="inproc_time").outputs_match is True

    res = evaluate(task, SUM_SOMETIMES_WRONG)
    assert res.verify_result["status"] == "proven"
    assert res.status == "bound_overfit", res
    assert reward(res) == 0.0


def test_large_tests_drop_inputs_where_original_has_ub():
    """Original overflows (UB) only at large n; a candidate differing there must not fail."""
    orig = "int pos_sq(int *a, int n) { int s = 0; for (int i = 0; i < n; i++) s += a[i] * a[i]; return s > 0; }"
    cand = ("int pos_sq(int *a, int n) { long long s = 0; for (int i = 0; i < n; i++)"
            " s += (long long)a[i] * a[i]; return s > 0; }")
    task = _task("int pos_sq(int *a, int n)", orig, "pos_sq",
                 input_ranges={"_narrowed": True, "_buf_range": [-15000, 15000]})
    tests = [testgen._generate_random_input(task.signature, task.pointer_rules, 512, task.input_ranges,
                                            min_len=9)]
    if testgen._original_ub_inputs(task, tests, 512, 30) is None:
        pytest.skip("UBSan unavailable")
    r = testgen.run_large_tests(task, cand)
    assert r.n_dropped_ub > 0
    assert r.verdict != "failed", r


def test_large_tests_not_applicable_for_scalar_only_functions():
    task = _task("int add_one(int x)", "int add_one(int x) { return x + 1; }", "add_one",
                 input_ranges={"_narrowed": True, "x": [-1000, 1000]})
    assert testgen.run_large_tests(task, "int add_one(int x) { return 1 + x; }").verdict == "not_applicable"


def test_large_input_generator_respects_min_len():
    task = _sum_task()
    import random
    rng = random.Random(0)
    for _ in range(50):
        inp = testgen._generate_random_input(task.signature, task.pointer_rules, 512, task.input_ranges,
                                             rng=rng, min_len=9)
        assert 9 <= inp["n"] <= 512 and len(inp["a"]) == 512


# --- P2 / P3: statuses ---------------------------------------------------------

def test_unknown_large_input_when_no_large_check_succeeds(monkeypatch):
    rw = sys.modules["equiv.reward"]
    import equiv.speed as speed

    monkeypatch.setattr(rw, "check_near_identical", lambda *a, **k: False)

    monkeypatch.setattr(testgen, "run_large_tests",
                        lambda *a, **k: testgen.LargeTestResult(verdict="error", details="forced"))
    monkeypatch.setattr(speed, "measure_speedup",
                        lambda *a, **k: speed.SpeedResult(error="forced", proxy="inproc_time"))
    res = evaluate(_sum_task(), SUM_SAME_ORDER, skip_tests=True)
    assert res.status == "unknown_large_input", res
    assert reward(res, base=0.1) == 0.0


def test_proven_survives_when_large_tests_pass_but_speed_errors(monkeypatch):
    rw = sys.modules["equiv.reward"]
    import equiv.speed as speed

    monkeypatch.setattr(rw, "check_near_identical", lambda *a, **k: False)

    monkeypatch.setattr(speed, "measure_speedup",
                        lambda *a, **k: speed.SpeedResult(error="forced", proxy="inproc_time"))
    res = evaluate(_sum_task(), SUM_SAME_ORDER, skip_tests=True)
    assert res.status == "proven", res
    assert res.checks["large_tests"] == "passed" and res.checks["checksum"] == "not_run"


# --- P5: analysis mode ------------------------------------------------------------

def test_short_circuit_false_runs_every_check_and_keeps_status():
    task = _sum_task()
    wrong = "int sum_arr(int *a, int n) { int s = 0; for (int i = 1; i < n; i++) s += a[i]; return s; }"
    fast = evaluate(task, wrong)
    full = evaluate(task, wrong, short_circuit=False)
    assert fast.status == full.status == "test_fail"
    assert "cbmc_main" not in fast.checks
    assert full.checks["cheap_tests"] == "fail"
    assert full.checks["heavy_tests"] == "fail"
    assert full.checks["cbmc_main"] == "not_equivalent"
    assert full.checks["large_tests"] == "failed"


def test_short_circuit_false_on_proven_candidate():
    res = evaluate(_sum_task(), SUM_SAME_ORDER, short_circuit=False, skip_speed=True)
    assert res.status == "proven", res
    assert res.checks == {"cheap_tests": "pass", "heavy_tests": "pass", "cbmc_main": "proven",
                          "cbmc_overlap": res.overlap_check, "large_tests": "passed"}


# --- P1: mutation-study escape rates ------------------------------------------------

def test_escape_rates():
    from run_mutation_study import escape_rates

    R = lambda cbmc, test, gt, replay="replayed": SimpleNamespace(
        cbmc_verdict=cbmc, test_verdict=test, ground_truth=gt, counterexample_replay=replay)
    results = [
        R("not_equivalent", "fail", "differs"),
        R("not_equivalent", "pass", "differs"),               # cheap escape
        R("not_equivalent", "pass", "same"),                  # escapes both
        R("not_equivalent", "pass", "same", "not_reproduced"),
        R("proven", "pass", "same", "not_applicable"),
    ]
    e = escape_rates(results)
    assert e["refuted"] == {"n": 4, "cheap_escape": 3, "cheap_escape_pct": 75.0,
                            "heavy_escape": 2, "heavy_escape_pct": 50.0,
                            "both_escape": 2, "both_escape_pct": 50.0}
    assert e["confirmed_refuted"]["n"] == 3
    assert e["n_passed_cheap"] == 4 and e["h_cheap_pct"] == 75.0
