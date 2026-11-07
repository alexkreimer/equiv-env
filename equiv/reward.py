"""
reward.py — Public API: evaluate() and reward().

Usage:
    from equiv.reward import evaluate, reward, EvalResult

    result = evaluate(task_id, candidate_src)
    r = reward(result)
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Literal

from .cache import EvalCache
from .extract import Task
from .verify import verify, VerifyResult, self_verify


EvalStatus = Literal[
    "proven",
    "not_equivalent",
    "unknown_timeout",
    "unknown_unwind",
    "unsupported",
    "compile_error",
    "checker_error",
    "test_fail",
]


@dataclass
class EvalResult:
    """Full evaluation result for a single candidate."""
    status: EvalStatus
    task_id: str
    speedup: float = 1.0
    check_time_s: float = 0.0
    test_result: dict | None = None
    verify_result: dict | None = None
    details: str = ""


# Global cache instance (lazy init)
_cache: EvalCache | None = None
_config_hash: str = ""


def init(config: dict | None = None, cache_path: str = "data/eval_cache.db"):
    """Initialize the reward module with config and cache."""
    global _cache, _config_hash
    _cache = EvalCache(cache_path)
    _config_hash = hashlib.sha256(
        (str(config) if config else "").encode()
    ).hexdigest()[:16]


def evaluate(
    task: Task,
    candidate_src: str,
    *,
    timeout_s: float = 60,
    maxn: int = 8,
    skip_tests: bool = False,
    extra_cbmc_flags: list[str] | None = None,
    _allow_overflow_off: bool = False,  # internal: only for self-equiv
) -> EvalResult:
    """Evaluate a candidate against the original function.

    Pipeline: compile → cheap tests → CBMC verify → speed proxy.

    Args:
        task: The eligible Task.
        candidate_src: The candidate function source code.
        timeout_s: Timeout per CBMC check.
        maxn: Maximum buffer size.
        skip_tests: Skip the cheap test pre-filter (for debugging).
        extra_cbmc_flags: Additional CBMC flags.

    Returns:
        EvalResult with status and timing information.
    """
    # Enforce: candidate checks MUST run with signed-overflow-check ON.
    # --no-signed-overflow-check is only allowed for self-equivalence.
    if extra_cbmc_flags and "--no-signed-overflow-check" in extra_cbmc_flags:
        if not _allow_overflow_off:
            raise ValueError(
                "--no-signed-overflow-check is only allowed for self-equivalence "
                "checks. Candidate checks must run with all safety checks ON."
            )

    start_time = time.monotonic()

    # Check cache
    if _cache and _config_hash:
        cached = _cache.get(task.task_id, candidate_src, _config_hash)
        if cached:
            return EvalResult(**cached)

    # 1. Normalize candidate
    candidate_src = candidate_src.strip()

    # 2. Cheap test pre-filter
    if not skip_tests:
        from .testgen import run_tests
        test_result = run_tests(task, candidate_src, maxn=maxn)
        if not test_result.passed:
            result = EvalResult(
                status="test_fail",
                task_id=task.task_id,
                check_time_s=test_result.time_s,
                test_result={"first_failure": test_result.first_failure,
                             "details": test_result.details},
                details=f"Failed cheap tests: {test_result.details[:200]}",
            )
            _cache_put(task.task_id, candidate_src, result)
            return result
    else:
        test_result = None

    # 3. CBMC verify
    verify_result = verify(
        task, candidate_src, timeout_s=timeout_s, maxn=maxn,
        extra_cbmc_flags=extra_cbmc_flags,
    )

    # Map VerifyResult status to EvalResult
    status_map = {
        "proven": "proven",
        "not_equivalent": "not_equivalent",
        "unknown_timeout": "unknown_timeout",
        "unknown_unwind": "unknown_unwind",
        "unsupported": "unsupported",
        "compile_error": "compile_error",
        "checker_error": "checker_error",
    }

    status = status_map.get(verify_result.status, "checker_error")
    speedup = 1.0

    # 4. Speed proxy (only if proven)
    if status == "proven":
        # Placeholder — speed.py will fill this in
        speedup = 1.0

    check_time = time.monotonic() - start_time

    result = EvalResult(
        status=status,
        task_id=task.task_id,
        speedup=speedup,
        check_time_s=check_time,
        test_result={"passed": test_result.passed, "n_tests": test_result.n_tests}
        if test_result else None,
        verify_result={
            "status": verify_result.status,
            "counterexample": verify_result.counterexample,
            "check_time_s": verify_result.check_time_s,
            "details": verify_result.details,
        },
        details=verify_result.details,
    )

    _cache_put(task.task_id, candidate_src, result)
    return result


def reward(r: EvalResult, cap: float = 4.0, base: float = 0.1) -> float:
    """Compute reward from evaluation result.

    Reward structure:
        - Not proven: 0.0
        - Proven but not faster: base (0.1)
        - Proven and faster: base + min(speedup, cap) - 1.0
    """
    if r.status != "proven":
        return 0.0
    if r.speedup <= 1.0:
        return base
    return base + min(r.speedup, cap) - 1.0


def _cache_put(task_id: str, candidate_src: str, result: EvalResult):
    """Cache the evaluation result if cache is configured."""
    if _cache and _config_hash:
        try:
            _cache.put(task_id, candidate_src, _config_hash, {
                "status": result.status,
                "task_id": result.task_id,
                "speedup": result.speedup,
                "check_time_s": result.check_time_s,
                "test_result": result.test_result,
                "verify_result": result.verify_result,
                "details": result.details,
            })
        except Exception:
            pass  # Cache failures shouldn't block evaluation


def evaluate_batch(
    tasks_candidates: list[tuple[Task, str]],
    *,
    timeout_s: float = 60,
    maxn: int = 8,
    n_workers: int = 1,
    extra_cbmc_flags: list[str] | None = None,
) -> list[EvalResult]:
    """Evaluate multiple (task, candidate) pairs.

    Uses a process pool when n_workers > 1. Does not crash on individual failures.
    """
    if n_workers <= 1:
        results = []
        for task, candidate_src in tasks_candidates:
            try:
                r = evaluate(task, candidate_src, timeout_s=timeout_s,
                             maxn=maxn, extra_cbmc_flags=extra_cbmc_flags)
            except Exception as e:
                r = EvalResult(
                    status="checker_error", task_id=task.task_id,
                    details=str(e),
                )
            results.append(r)
        return results

    from concurrent.futures import ProcessPoolExecutor, as_completed
    results = [None] * len(tasks_candidates)

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        future_to_idx = {}
        for i, (task, cand) in enumerate(tasks_candidates):
            future = executor.submit(
                evaluate, task, cand,
                timeout_s=timeout_s, maxn=maxn,
                extra_cbmc_flags=extra_cbmc_flags,
            )
            future_to_idx[future] = i

        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                # We don't have the task_id here — use empty string
                results[idx] = EvalResult(
                    status="checker_error", task_id="",
                    details=str(e),
                )

    return results