"""
verify.py — Run CBMC on an equivalence harness and parse the result.

Usage:
    from equiv.verify import verify, VerifyResult
    result = verify(task, candidate_src, timeout_s=60)
"""

from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Literal

from .extract import Task
from .harness import generate_harness, HarnessResult


VerifyStatus = Literal[
    "proven",
    "not_equivalent",
    "unknown_timeout",
    "unknown_unwind",
    "unsupported",
    "compile_error",
    "checker_error",
]


@dataclass
class VerifyResult:
    status: VerifyStatus
    counterexample: dict | None = None  # input values + outputs, parsed from trace
    check_time_s: float = 0.0
    cbmc_version: str = ""
    details: str = ""  # short raw message for debugging


# CBMC exit codes
EXIT_SUCCESS = 0
EXIT_FAILURE = 10  # property violation found
EXIT_ERROR = 6     # parsing/compilation error
EXIT_SIGNAL = -1   # killed by signal (timeout)


def verify(
    task: Task,
    candidate_src: str,
    *,
    timeout_s: float = 60,
    maxn: int = 8,
    unwind: int | None = None,
    extra_cbmc_flags: list[str] | None = None,
) -> VerifyResult:
    """Verify equivalence between original and candidate using CBMC.

    Args:
        task: The eligible Task from extract.py.
        candidate_src: The candidate function source code.
        timeout_s: Hard timeout per CBMC run (enforced via subprocess).
        maxn: Maximum buffer size.
        unwind: Loop unwind bound (default: maxn + 2).
        extra_cbmc_flags: Additional CBMC flags (e.g. ["--no-signed-overflow-check"]).

    Returns:
        VerifyResult with status and optional counterexample.
    """
    if unwind is None:
        unwind = maxn + 2

    # Generate harness
    harness_result = generate_harness(task, candidate_src, maxn=maxn, unwind=unwind)
    harness_src = harness_result.harness_src

    # Write to temp file
    with tempfile.NamedTemporaryFile(
        suffix=".c", mode="w", prefix="equiv_", delete=False
    ) as f:
        f.write(harness_src)
        harness_path = f.name

    try:
        result = _run_cbmc(
            harness_path,
            task,
            harness_result,
            timeout_s=timeout_s,
            unwind=unwind,
            extra_cbmc_flags=extra_cbmc_flags,
        )
    finally:
        try:
            os.unlink(harness_path)
        except OSError:
            pass

    return result


def self_verify(
    task: Task,
    *,
    timeout_s: float = 60,
    maxn: int = 8,
    unwind: int | None = None,
    extra_cbmc_flags: list[str] | None = None,
) -> VerifyResult:
    """Verify self-equivalence (original vs identical copy).

    Used during dataset building to check that CBMC can handle the function
    within the timeout.
    """
    from .harness import generate_self_harness
    return verify(task, task.func_src, timeout_s=timeout_s, maxn=maxn,
                  unwind=unwind, extra_cbmc_flags=extra_cbmc_flags)


def _run_cbmc(
    harness_path: str,
    task: Task,
    harness_result: HarnessResult,
    *,
    timeout_s: float,
    unwind: int,
    extra_cbmc_flags: list[str] | None,
) -> VerifyResult:
    """Run CBMC and parse the output."""
    flags = [
        "cbmc",
        "--function", "harness",
        "--unwind", str(unwind),
        "--unwinding-assertions",
    ]

    # Add extra flags (e.g. --no-signed-overflow-check for self-equiv)
    if extra_cbmc_flags:
        flags.extend(extra_cbmc_flags)

    flags.append(harness_path)

    start_time = time.monotonic()

    try:
        proc = subprocess.run(
            flags,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            # Start in new process group for clean kill on timeout
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )
    except subprocess.TimeoutExpired:
        check_time = time.monotonic() - start_time
        return VerifyResult(
            status="unknown_timeout",
            check_time_s=check_time,
            details=f"Timeout after {timeout_s}s",
        )

    check_time = time.monotonic() - start_time
    stdout = proc.stdout
    stderr = proc.stderr

    # Get CBMC version from first line
    version = ""
    for line in stdout.split("\n"):
        if "CBMC version" in line:
            version = line.strip()
            break

    # Check for errors
    if proc.returncode == EXIT_ERROR or "PARSING ERROR" in stderr or "CONVERSION ERROR" in stderr:
        return VerifyResult(
            status="compile_error",
            check_time_s=check_time,
            cbmc_version=version,
            details=stderr[:500] if stderr else stdout[:500],
        )

    # Check for signal death
    if proc.returncode < 0:
        return VerifyResult(
            status="checker_error",
            check_time_s=check_time,
            cbmc_version=version,
            details=f"CBMC killed by signal {-proc.returncode}",
        )

    # Parse results
    status, details = _parse_cbmc_output(stdout)

    # On success, we're done
    if status == "proven":
        return VerifyResult(
            status="proven",
            check_time_s=check_time,
            cbmc_version=version,
            details=details,
        )

    # On failure, re-run with --trace to get counterexample
    if status == "not_equivalent":
        ce = _get_counterexample(harness_path, unwind, extra_cbmc_flags, timeout_s)
        return VerifyResult(
            status="not_equivalent",
            counterexample=ce,
            check_time_s=check_time,
            cbmc_version=version,
            details=details,
        )

    # Unknown (unwind failure, etc.)
    if "unwind" in details.lower() or "unwinding" in details.lower():
        status = "unknown_unwind"

    return VerifyResult(
        status=status,
        check_time_s=check_time,
        cbmc_version=version,
        details=details,
    )


def _parse_cbmc_output(stdout: str) -> tuple[VerifyStatus, str]:
    """Parse CBMC stdout to determine verification status."""
    if "VERIFICATION SUCCESSFUL" in stdout:
        # Count failures (should be 0)
        failure_count = stdout.count("FAILURE")
        if failure_count == 0:
            return "proven", "All properties hold"

    if "VERIFICATION FAILED" in stdout:
        # Determine WHY it failed
        # Check for assertion failures (not_equivalent)
        assertion_failures = re.findall(r'\[(\w+)\.assertion\.\d+\]', stdout)
        if assertion_failures:
            failed_assertions = [a for a in assertion_failures if "FAILURE" in stdout]
            # Need to check if assertion properties actually FAILED
            # Re-scan with more context
            if _has_failed_assertions(stdout):
                return "not_equivalent", f"Assertion failure(s) found"

        # Check for unwinding assertion failures
        unwind_failures = re.findall(r'\[.*\.unwind\.\d+\].*FAILURE', stdout)
        if unwind_failures:
            return "unknown_unwind", "Unwinding assertion(s) failed"

        # Check for other property failures (overflow, pointer, bounds, etc.)
        safety_failures = re.findall(
            r'\[.*\.(overflow|pointer|bounds|shift|conversion|div-by-zero)\.\d+\].*FAILURE',
            stdout,
        )
        if safety_failures:
            # Safety failure in candidate = not_equivalent (candidate has UB)
            # Safety failure in original = checker_error (should have been filtered)
            candidate_safety = [s for s in safety_failures if "cand__" in s]
            if candidate_safety:
                return "not_equivalent", f"Candidate has UB: {candidate_safety[:3]}"
            return "checker_error", f"Original has UB: {safety_failures[:3]}"

        # Generic failure
        return "not_equivalent", "Verification failed"

    if "PARSING ERROR" in stdout or "CONVERSION ERROR" in stdout:
        return "compile_error", stdout[:500]

    # Unknown outcome
    return "checker_error", stdout[:500]


def _has_failed_assertions(stdout: str) -> bool:
    """Check if any assertion property actually failed (not just SUCCESS)."""
    lines = stdout.split("\n")
    for line in lines:
        if "assertion" in line and "FAILURE" in line:
            return True
    return False


def _get_counterexample(
    harness_path: str,
    unwind: int,
    extra_cbmc_flags: list[str] | None,
    timeout_s: float,
) -> dict | None:
    """Re-run CBMC with --trace to get counterexample input values."""
    flags = [
        "cbmc",
        "--function", "harness",
        "--unwind", str(unwind),
        "--unwinding-assertions",
        "--trace",
    ]
    if extra_cbmc_flags:
        flags.extend(extra_cbmc_flags)
    flags.append(harness_path)

    try:
        proc = subprocess.run(
            flags,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {"error": "Timeout during counterexample extraction"}

    return _parse_trace(proc.stdout)


def _parse_trace(stdout: str) -> dict | None:
    """Parse CBMC trace output to extract counterexample values.

    Returns a dict with input variable names mapped to their values.
    """
    ce: dict = {"inputs": {}}

    # Extract state assignments from the trace
    # Pattern: State N file ... line L ... variable=value
    # or: x=151519248 (00001001 00001000 00000000 00010000)
    trace_started = False
    for line in stdout.split("\n"):
        if "Trace for" in line:
            trace_started = True
            continue

        if not trace_started:
            continue

        # Match: variable_name=value (binary)
        match = re.search(r'^\s*(\w+)=(-?\d+)\s', line)
        if match:
            name = match.group(1)
            value = match.group(2)
            # Skip internal CBMC variables
            if name.startswith("goto_symex") or name.startswith("return_value"):
                continue
            if name.startswith("__"):
                continue
            if name not in ce["inputs"]:
                ce["inputs"][name] = value

        # Match: return_value_orig__NAME=value
        match_ret = re.search(r'return_value_orig__(\w+)=(-?\d+)', line)
        if match_ret:
            ce["original_return"] = int(match_ret.group(2))

        match_ret = re.search(r'return_value_cand__(\w+)=(-?\d+)', line)
        if match_ret:
            ce["candidate_return"] = int(match_ret.group(2))

        # Stop at "Violated property"
        if "Violated property" in line:
            break

    return ce if ce.get("inputs") else None


def verify_batch(
    tasks_candidates: list[tuple[Task, str]],
    *,
    timeout_s: float = 60,
    maxn: int = 8,
    n_workers: int = 1,
    extra_cbmc_flags: list[str] | None = None,
) -> list[VerifyResult]:
    """Verify multiple (task, candidate) pairs, optionally in parallel."""
    if n_workers <= 1:
        results = []
        for task, candidate_src in tasks_candidates:
            try:
                r = verify(task, candidate_src, timeout_s=timeout_s,
                           maxn=maxn, extra_cbmc_flags=extra_cbmc_flags)
            except Exception as e:
                r = VerifyResult(status="checker_error", details=str(e))
            results.append(r)
        return results

    # Parallel execution
    from concurrent.futures import ProcessPoolExecutor, as_completed
    results = [None] * len(tasks_candidates)

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        future_to_idx = {}
        for i, (task, cand) in enumerate(tasks_candidates):
            future = executor.submit(
                verify, task, cand,
                timeout_s=timeout_s, maxn=maxn,
                extra_cbmc_flags=extra_cbmc_flags,
            )
            future_to_idx[future] = i

        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                results[idx] = VerifyResult(status="checker_error", details=str(e))

    return results