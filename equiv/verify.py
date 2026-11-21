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


def run_cbmc_process(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess:
    """Run CBMC in its own session; on timeout, kill the WHOLE process group.

    subprocess.run(timeout=...) kills only the direct child. CBMC started with
    an external solver (--z3, --smt2, --bitwuzla) spawns it as a child, which
    would survive the timeout and keep burning CPU. Raises TimeoutExpired, like
    subprocess.run.
    """
    import signal
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


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
        "--arch", "x86_64",
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
        proc = run_cbmc_process(flags, timeout_s)
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
        "--arch", "x86_64",
        "--function", "harness",
        "--unwind", str(unwind),
        "--unwinding-assertions",
        "--trace",
    ]
    if extra_cbmc_flags:
        flags.extend(extra_cbmc_flags)
    flags.append(harness_path)

    try:
        proc = run_cbmc_process(flags, timeout_s)
    except subprocess.TimeoutExpired:
        return {"error": "Timeout during counterexample extraction"}

    return _parse_trace(proc.stdout)


_TRACE_STATE_RE = re.compile(r'^State \d+ .*? function (\S+)')
_TRACE_ASSIGN_RE = re.compile(r'^\s*([A-Za-z_][\w$]*)(?:\[(\d+)[a-z]*\])?=(.*)$')
_TRACE_TOKEN_RE = re.compile(r"'(?:\\.|[^'\\])'|[^,\s{}]+")
_TRACE_HEAD_VAL_RE = re.compile(r"^('(?:\\.|[^'\\])'|\S+)")
_TRACE_INT_RE = re.compile(r'^(-?\d+)(?:[uUlL]+)?$')


def _first_trace_token(rest: str) -> str:
    """Extract the leading value token (preserving `' '` space char literals)."""
    m = _TRACE_HEAD_VAL_RE.match(rest.lstrip())
    return m.group(1) if m else ""


def _parse_trace_value(tok: str) -> int | None:
    """Parse one CBMC trace value token (`8`, `-1`, `4294967295u`, `'a'`, `TRUE`)."""
    tok = tok.strip()
    m = _TRACE_INT_RE.match(tok)
    if m:
        return int(m.group(1))
    if tok == "TRUE":
        return 1
    if tok == "FALSE":
        return 0
    if len(tok) >= 3 and tok[0] == "'" and tok[-1] == "'":
        body = tok[1:-1]
        try:
            return ord(body.encode().decode("unicode_escape")) if body.startswith("\\") else ord(body)
        except (TypeError, ValueError):
            return None
    return None


def _parse_trace(stdout: str) -> dict | None:
    """Parse the first CBMC counterexample trace.

    Returns a dict:
      inputs:   {scalar_param: "value"}
      buffers:  {pointer_param: [int, ...]}   (contents *before* the call)
      kind:     "mismatch" (an equivalence assertion failed) or "ub" (a CBMC
                safety property failed, e.g. out-of-bounds in the candidate)
      violated_property: CBMC property id, e.g. "harness.assertion.1"
      original_return / candidate_return: ints when present

    Inputs/buffers are recorded only while the trace is still inside
    `harness` (i.e. before orig__/cand__ run), because the functions under
    test may overwrite their buffers and we need the *initial* values.
    """
    ce: dict = {"inputs": {}, "buffers": {}}
    trace_started = False
    in_harness = True

    for line in stdout.split("\n"):
        m_trace = re.match(r'^Trace for (\S+?):', line)
        if m_trace:
            if trace_started:      # only the first failing property
                break
            trace_started = True
            prop = m_trace.group(1)
            ce["violated_property"] = prop
            ce["kind"] = "mismatch" if ".assertion." in prop else "ub"
            continue
        if not trace_started:
            continue

        m_state = _TRACE_STATE_RE.match(line)
        if m_state:
            if m_state.group(1) != "harness":
                in_harness = False
            continue

        match_ret = re.search(r'return_value_orig__\w+=(-?\d+)', line)
        if match_ret:
            ce["original_return"] = int(match_ret.group(1))
        match_ret = re.search(r'return_value_cand__\w+=(-?\d+)', line)
        if match_ret:
            ce["candidate_return"] = int(match_ret.group(1))

        if "Violated property" in line:
            break
        if not in_harness:
            continue

        m = _TRACE_ASSIGN_RE.match(line)
        if not m:
            continue
        name, idx, rest = m.group(1), m.group(2), m.group(3)
        if name.startswith(("goto_symex", "return_value", "__")) or "$" in name:
            continue

        if name.endswith("_c") or name.endswith("_m"):
            continue                       # candidate copies mirror the originals
        if name.endswith("_o"):
            param = name[:-2]
            buf = ce["buffers"].setdefault(param, [])
            if idx is not None:            # element assignment: a_o[3l]=5 or a_o[0l]=' '
                i = int(idx)
                val = _parse_trace_value(_first_trace_token(rest))
                if val is not None:
                    if len(buf) <= i:
                        buf.extend([0] * (i + 1 - len(buf)))
                    buf[i] = val
            elif rest.lstrip().startswith("{"):   # whole array: a_o={ 0, 1 } (...)
                body = rest.lstrip()[1:]
                body = body.split("}", 1)[0]
                vals = [_parse_trace_value(t) for t in _TRACE_TOKEN_RE.findall(body)]
                ce["buffers"][param] = [v if v is not None else 0 for v in vals]
            continue

        if idx is None and name not in ce["inputs"]:
            val = _parse_trace_value(_first_trace_token(rest))
            if val is not None:
                ce["inputs"][name] = str(val)

    return ce if (ce["inputs"] or ce["buffers"]) else None


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


def check_original_ub(
    task: Task,
    *,
    timeout_s: float = 60,
    maxn: int = 8,
    unwind: int | None = None,
) -> tuple[bool, dict | None]:
    """Run CBMC on the original function alone with all safety checks ON.

    Uses `generate_ub_harness()` so the UB check sees the exact same headers,
    string null-terminators, and paired length constraints as equivalence
    checking. If the original has undefined behavior, retries once with
    type-safe narrowed input ranges ([-2^15, 2^15 - 1] clamped to type bounds).

    Returns:
        (ub_free, input_ranges) — ub_free=True means no UB found.
        input_ranges is None if full range is safe, or a dict with '_narrowed': True.
    """
    if unwind is None:
        unwind = task.unwind or (maxn + 2)

    from .harness import generate_ub_harness

    def _run_ub(use_narrow: bool) -> subprocess.CompletedProcess | None:
        harness = generate_ub_harness(
            task, maxn=maxn, unwind=unwind, use_narrow_ranges=use_narrow
        )
        with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
            f.write(harness.harness_src)
            harness_path = f.name
        try:
            return _run_cbmc_raw(harness_path, unwind, timeout_s, extra_flags=None)
        except subprocess.TimeoutExpired:
            return None
        finally:
            try:
                os.unlink(harness_path)
            except OSError:
                pass

    def _is_parse_error(res: subprocess.CompletedProcess) -> bool:
        return (
            res.returncode == EXIT_ERROR
            or "PARSING ERROR" in res.stderr
            or "CONVERSION ERROR" in res.stderr
        )

    result = _run_ub(use_narrow=False)
    if result is None or _is_parse_error(result):
        return False, None

    if "VERIFICATION SUCCESSFUL" in result.stdout:
        return True, None

    if _has_safety_failures(result.stdout):
        # Stage 1: retry with narrowed ranges [-2^15, 2^15 - 1]
        result_narrow = _run_ub(use_narrow=True)
        if result_narrow is not None and not _is_parse_error(result_narrow):
            if not _has_safety_failures(result_narrow.stdout):
                return True, _narrow_ranges(task)

        # Stage 2: retry with positive small scalar ranges [1, 128], paired lengths
        # [1, MAXN - 1], and buffer elements [-128, 128]
        from dataclasses import replace as _dc_replace
        stage2_ranges = _narrow_ranges_stage2(task, maxn=maxn)
        task_stage2 = _dc_replace(task, input_ranges=stage2_ranges)
        harness_s2 = generate_ub_harness(task_stage2, maxn=maxn, unwind=unwind)
        with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
            f.write(harness_s2.harness_src)
            s2_path = f.name
        try:
            res_s2 = _run_cbmc_raw(s2_path, unwind, timeout_s, extra_flags=None)
        except subprocess.TimeoutExpired:
            res_s2 = None
        finally:
            try:
                os.unlink(s2_path)
            except OSError:
                pass

        if res_s2 is None or _is_parse_error(res_s2) or _has_safety_failures(res_s2.stdout):
            return False, None  # ub_in_original
        return True, stage2_ranges

    # Only unwinding assertion failed (no safety UB detected at this unwind);
    # caller's unwind search will resolve the unwind bound and re-check if needed.
    return True, None


def _build_ub_check_harness(task: Task, maxn: int, use_narrow_ranges: bool) -> str:
    """Backward-compatible wrapper delegating to harness.generate_ub_harness."""
    from .harness import generate_ub_harness
    return generate_ub_harness(
        task, maxn=maxn, use_narrow_ranges=use_narrow_ranges
    ).harness_src


def _run_cbmc_raw(
    harness_path: str, unwind: int, timeout_s: float, extra_flags: list[str] | None
) -> subprocess.CompletedProcess:
    """Run CBMC and return the raw result."""
    flags = [
        "cbmc", "--arch", "x86_64",
        "--function", "harness",
        "--unwind", str(unwind),
        "--unwinding-assertions",
    ]
    if extra_flags:
        flags.extend(extra_flags)
    flags.append(harness_path)

    return run_cbmc_process(flags, timeout_s)


def _has_safety_failures(stdout: str) -> bool:
    """Check if any non-assertion safety property failed."""
    patterns = [
        r'\[.*\.overflow\.\d+\].*FAILURE',
        r'\[.*\.pointer_dereference\.\d+\].*FAILURE',
        r'\[.*\.pointer_arithmetic\.\d+\].*FAILURE',
        r'\[.*\.array_bounds\.\d+\].*FAILURE',
        r'\[.*\.undefined-shift\.\d+\].*FAILURE',
        r'\[.*\.div-by-zero\.\d+\].*FAILURE',
    ]
    for p in patterns:
        if re.search(p, stdout):
            return True
    return False


def _narrow_ranges(task: Task) -> dict:
    """Build Stage 1 type-safe narrowed input ranges (excluding paired length params)."""
    from .extract import get_paired_length_params
    from .harness import _type_bounds

    paired_lengths = get_paired_length_params(task.signature, task.pointer_rules)
    ranges: dict = {"_narrowed": True, "_buf_range": [-32768, 32767]}
    for name, c_type in task.signature.params:
        if c_type.strip().endswith("*") or name in paired_lengths:
            continue
        tmin, tmax, _ = _type_bounds(c_type)
        lo = max(-32768, tmin)
        hi = min(32767, tmax)
        ranges[name] = [lo, hi]
    return ranges


def _narrow_ranges_stage2(task: Task, maxn: int = 8) -> dict:
    """Build Stage 2 narrowed ranges: non-zero positive scalars [1, 128],
    paired lengths [1, maxn - 1], and buffer elements [-128, 128]."""
    from .extract import get_paired_length_params
    from .harness import _type_bounds

    paired_lengths = get_paired_length_params(task.signature, task.pointer_rules)
    ranges: dict = {"_narrowed": True, "_buf_range": [-128, 128]}
    for name, c_type in task.signature.params:
        if c_type.strip().endswith("*"):
            continue
        if name in paired_lengths:
            ranges[name] = [1, max(1, maxn - 1)]
        else:
            tmin, tmax, _ = _type_bounds(c_type)
            ranges[name] = [max(1, tmin), min(128, tmax)]
    return ranges