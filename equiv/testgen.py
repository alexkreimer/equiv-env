"""
testgen.py — Cheap IO/random test pre-filter for candidate functions.

Runs the original and candidate on test inputs (ExeBench IO pairs + random +
edge cases) and checks output equality. Most wrong candidates die here
in milliseconds, before the expensive CBMC check.

Usage:
    from equiv.testgen import run_tests, TestResult
"""

from __future__ import annotations

import hashlib
import random
import subprocess
import tempfile
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .extract import Task
from .harness import generate_harness, NONDET_DECLS, HEADER, _pointee_type


@dataclass
class TestResult:
    passed: bool
    first_failure: dict | None = None  # input that caused failure
    time_s: float = 0.0
    n_tests: int = 0
    details: str = ""


# Edge value sets by integer width
EDGE_VALUES = {
    "int": [0, 1, -1, 2147483647, -2147483648, 2, -2, 42, -42],
    "unsigned int": [0, 1, 4294967295, 2, 42],
    "char": [0, 1, -1, 127, -128],
    "unsigned char": [0, 1, 255, 2],
    "short": [0, 1, -1, 32767, -32768],
    "unsigned short": [0, 1, 65535],
    "long long": [0, 1, -1, 9223372036854775807, -9223372036854775808],
    "unsigned long long": [0, 1, 18446744073709551615],
    "size_t": [0, 1, 2, 8, 255],
}


def run_tests(
    task: Task,
    candidate_src: str,
    *,
    maxn: int = 8,
    n_random: int = 200,
    include_edges: bool = True,
    timeout_s: float = 30,
    seed: int | None = None,
) -> TestResult:
    """Run cheap IO tests on a candidate function.

    Tests = ExeBench IO pairs (if available) + random inputs + edge values.
    Expected outputs come from running the original.

    Args:
        task: The eligible Task.
        candidate_src: The candidate function source.
        maxn: Maximum buffer size for pointer parameters.
        n_random: Number of random test inputs to generate.
        include_edges: Whether to include edge values (0, 1, -1, INT_MIN, etc.).
        timeout_s: Hard timeout for compilation + running all tests.
        seed: If given, random inputs come from a private seeded RNG (reproducible).

    Returns:
        TestResult with pass/fail and first failure details.
    """
    start_time = time.monotonic()

    # 1. Generate test inputs
    tests = []

    # ExeBench IO pairs
    if task.io_tests:
        for io_pair in task.io_tests:
            tests.append(io_pair["input"])

    # Random inputs
    sig = task.signature
    rng = random.Random(seed) if seed is not None else random
    for _ in range(n_random):
        test_input = _generate_random_input(sig, task.pointer_rules, maxn, task.input_ranges, rng=rng)
        tests.append(test_input)

    # Edge values
    if include_edges:
        edge_inputs = _generate_edge_inputs(sig, task.pointer_rules, maxn, task.input_ranges)
        tests.extend(edge_inputs)

    if not tests:
        return TestResult(passed=True, n_tests=0, details="No tests generated")

    # 2. Build a test driver that runs original and candidate on each input
    driver_src = _build_test_driver(task, candidate_src, tests, maxn)

    # 3. Compile and run
    with tempfile.NamedTemporaryFile(
        suffix=".c", mode="w", prefix="test_", delete=False
    ) as f:
        f.write(driver_src)
        driver_path = f.name

    try:
        result = _compile_and_run(driver_path, task, timeout_s)
    finally:
        try:
            os.unlink(driver_path)
        except OSError:
            pass

    result.time_s = time.monotonic() - start_time
    return result


def _generate_random_input(
    sig, pointer_rules: dict, maxn: int, input_ranges: dict | None = None,
    *, rng=random, min_len: int = 0,
) -> dict:
    """Generate one set of random inputs matching the function signature,
    respecting paired length bounds [min_len, maxn] and task.input_ranges.

    min_len > 0 is the large-input mode (run_large_tests): paired lengths and
    string lengths are drawn from [min_len, maxn] and take priority over
    task.input_ranges, matching the speed benchmark, which passes BENCH_N.
    """
    from .extract import get_paired_length_params
    from .harness import _type_bounds

    paired_lengths = get_paired_length_params(sig, pointer_rules)
    ranges = input_ranges or {}
    narrowed = bool(ranges.get("_narrowed"))
    buf_range = ranges.get("_buf_range", (-32768, 32767))

    inp = {}
    for name, c_type in sig.params:
        if c_type.strip().endswith("*"):
            pointee = _pointee_type(c_type)
            if "char" in pointee and pointer_rules.get(name) == "string":
                length = rng.randint(min(min_len, maxn - 1), maxn - 1)
                arr = [rng.randint(32, 126) for _ in range(length)] + [0]
                arr = arr[:maxn]
                while len(arr) < maxn:
                    arr.append(0)
            else:
                if narrowed:
                    tmin, tmax, _ = _type_bounds(pointee)
                    lo = max(int(buf_range[0]), tmin)
                    hi = min(int(buf_range[1]), tmax)
                    arr = [rng.randint(lo, hi) for _ in range(maxn)]
                else:
                    arr = _random_scalar_array(pointee, maxn, rng)
            inp[name] = arr
        elif min_len > 0 and name in paired_lengths:
            inp[name] = rng.randint(min_len, maxn)
        elif name in ranges and isinstance(ranges[name], (list, tuple)) and len(ranges[name]) == 2:
            inp[name] = rng.randint(int(ranges[name][0]), int(ranges[name][1]))
        elif name in paired_lengths:
            inp[name] = rng.randint(0, maxn)
        elif narrowed:
            tmin, tmax, _ = _type_bounds(c_type)
            inp[name] = rng.randint(max(-32768, tmin), min(32767, tmax))
        else:
            inp[name] = _random_scalar(c_type, rng)
    return inp


def _generate_edge_inputs(
    sig, pointer_rules: dict, maxn: int, input_ranges: dict | None = None
) -> list[dict]:
    """Generate edge-case test inputs (0, 1, -1, bounds, empty/full buffers)."""
    from .extract import get_paired_length_params
    from .harness import _type_bounds

    paired_lengths = get_paired_length_params(sig, pointer_rules)
    ranges = input_ranges or {}
    narrowed = bool(ranges.get("_narrowed"))
    inputs = []

    def _default_scalar(n2: str, t2: str) -> int:
        if n2 in ranges and isinstance(ranges[n2], (list, tuple)) and len(ranges[n2]) == 2:
            return int(ranges[n2][0])
        if n2 in paired_lengths:
            return min(1, maxn)
        return 0

    def _default_buf(n2: str, t2: str) -> list[int]:
        if pointer_rules.get(n2) == "string":
            return [0] * maxn
        return [0] * maxn

    # Collect edge scalars per type
    for name, c_type in sig.params:
        if not c_type.strip().endswith("*"):
            if name in ranges and isinstance(ranges[name], (list, tuple)) and len(ranges[name]) == 2:
                lo, hi = int(ranges[name][0]), int(ranges[name][1])
                base_type = c_type.replace("const ", "").strip()
                raw_edges = EDGE_VALUES.get(base_type, [0, 1, -1])
                edges = sorted(set([v for v in raw_edges if lo <= v <= hi] + [lo, hi]))
            elif name in paired_lengths:
                edges = sorted({0, 1, maxn // 2, max(0, maxn - 1), maxn})
            else:
                base_type = c_type.replace("const ", "").strip()
                raw_edges = EDGE_VALUES.get(base_type, [0, 1, -1])
                if narrowed:
                    tmin, tmax, _ = _type_bounds(c_type)
                    lo, hi = max(-32768, tmin), min(32767, tmax)
                    edges = [v for v in raw_edges if lo <= v <= hi] + [lo, hi]
                else:
                    edges = raw_edges
            for val in edges:
                inp = {}
                for n2, t2 in sig.params:
                    if n2 == name:
                        inp[n2] = val
                    elif t2.strip().endswith("*"):
                        inp[n2] = _default_buf(n2, t2)
                    else:
                        inp[n2] = _default_scalar(n2, t2)
                inputs.append(inp)

    # Empty and full buffers for pointer params
    for name, c_type in sig.params:
        if c_type.strip().endswith("*"):
            pointee = _pointee_type(c_type)
            # Empty buffer (all zeros)
            inp = {}
            for n2, t2 in sig.params:
                if n2 == name:
                    inp[n2] = [0] * maxn
                elif t2.strip().endswith("*"):
                    inp[n2] = ([65] * (maxn - 1) + [0]) if pointer_rules.get(n2) == "string" else [1] * maxn
                else:
                    inp[n2] = _default_scalar(n2, t2)
            inputs.append(inp)

            # Full buffer (max values, null-terminated if string)
            inp = {}
            for n2, t2 in sig.params:
                if n2 == name:
                    if pointer_rules.get(n2) == "string":
                        inp[n2] = [126] * (maxn - 1) + [0]
                    elif "char" in pointee:
                        inp[n2] = [127] * maxn
                    elif "unsigned" in c_type:
                        inp[n2] = [255] * maxn
                    else:
                        val = 32767 if narrowed else 1000000
                        inp[n2] = [val] * maxn
                elif t2.strip().endswith("*"):
                    inp[n2] = ([65] * (maxn - 1) + [0]) if pointer_rules.get(n2) == "string" else [1] * maxn
                else:
                    inp[n2] = _default_scalar(n2, t2)
            inputs.append(inp)

    return inputs[:50]  # cap edges


def _random_scalar(c_type: str, rng=random) -> int:
    """Generate a random value for a scalar type."""
    base = c_type.replace("const ", "").strip()
    if base in ("char", "unsigned char", "int8_t", "uint8_t", "bool", "_Bool"):
        return rng.randint(-128, 127)
    elif base in ("short", "unsigned short", "int16_t", "uint16_t"):
        return rng.randint(-32768, 32767)
    elif base in ("size_t", "unsigned int", "uint32_t"):
        return rng.randint(0, 1000000)
    else:
        return rng.randint(-1000000, 1000000)


def _random_scalar_array(pointee_type: str, maxn: int, rng=random) -> list[int]:
    """Generate a random array of scalar values."""
    if pointee_type in ("char", "unsigned char"):
        return [rng.randint(-128, 127) for _ in range(maxn)]
    elif pointee_type in ("short", "unsigned short"):
        return [rng.randint(-32768, 32767) for _ in range(maxn)]
    else:
        return [rng.randint(-1000000, 1000000) for _ in range(maxn)]


def _build_test_driver(
    task: Task, candidate_src: str, tests: list[dict], maxn: int,
    *, orig_only: bool = False,
) -> str:
    """Build a C test driver that runs original and candidate on each test case.

    orig_only=True builds a driver that runs only the original, each test in a
    forked child (5 s alarm), and prints "@UB <idx>" for every child that does
    not exit cleanly. Built with -fno-sanitize-recover, a UBSan hit aborts the
    child, so this finds every input on which the original has UB or crashes.
    (One process per test is needed: UBSan reports each source location only
    once per process.) Used by run_large_tests.
    """
    parts = []

    parts.append(HEADER)
    parts.append(f"#define MAXN {maxn}")

    # Include deps (filtered)
    from .harness import _filter_deps
    if task.deps_src:
        filtered = _filter_deps(task.deps_src)
        if filtered:
            parts.append(filtered)

    # Standard fixed-width types are already provided by <stdint.h> in HEADER

    # Renamed original and candidate
    from .harness import _rename_function, _extract_function_name
    orig = _rename_function(task.func_src, task.func_name, f"orig__{task.func_name}")
    if orig is None:
        orig = task.func_src
    parts.append(orig)

    if not orig_only:
        cand_name = _extract_function_name(candidate_src) or task.func_name
        cand = _rename_function(candidate_src, cand_name, f"cand__{task.func_name}")
        if cand is None:
            cand = candidate_src
        parts.append(cand)

    # Test runner
    parts.append(f"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
{"#include <unistd.h>" + chr(10) + "#include <sys/wait.h>" if orig_only else ""}

int main(void) {{
    int n_failed = 0;
    int n_tests = {len(tests)};
""")

    sig = task.signature
    for test_idx, test_input in enumerate(tests):
        parts.append(f"  {{ /* Test {test_idx} */")
        if orig_only:
            parts.append("   pid_t __pid = fork();")
            parts.append("   if (__pid == 0) { alarm(5);")

        # Declare and initialize inputs
        for name, c_type in sig.params:
            val = test_input.get(name, 0)
            if isinstance(val, list):
                pointee = _pointee_type(c_type)
                init = ", ".join(f"({pointee})({v})" for v in val) or "0"
                parts.append(f"    {pointee} {name}_o[MAXN] = {{{init}}};")
                parts.append(f"    {pointee} {name}_c[MAXN];")
                parts.append(f"    memcpy({name}_c, {name}_o, sizeof {name}_o);")
            else:
                parts.append(f"    {c_type} {name} = {val};")

        # Call both
        orig_args = []
        cand_args = []
        for name, c_type in sig.params:
            if isinstance(test_input.get(name, 0), list):
                orig_args.append(f"{name}_o")
                cand_args.append(f"{name}_c")
            else:
                orig_args.append(name)
                cand_args.append(name)

        ret_is_void = sig.return_type == "void"
        if orig_only:
            parts.append(f"    orig__{task.func_name}({', '.join(orig_args)});")
            parts.append("    _exit(0); }")
            parts.append("   int __st = 0; if (__pid < 0 || waitpid(__pid, &__st, 0) < 0"
                         " || !WIFEXITED(__st) || WEXITSTATUS(__st) != 0)")
            parts.append(f'     printf("@UB {test_idx}\\n");')
            parts.append("   (void)n_failed;")
            parts.append("  }")
            continue
        if not ret_is_void:
            parts.append(f"    {sig.return_type} r_o = orig__{task.func_name}({', '.join(orig_args)});")
            parts.append(f"    {sig.return_type} r_c = cand__{task.func_name}({', '.join(cand_args)});")
        else:
            parts.append(f"    orig__{task.func_name}({', '.join(orig_args)});")
            parts.append(f"    cand__{task.func_name}({', '.join(cand_args)});")

        # Check results
        if not ret_is_void:
            parts.append(f"    if (r_o != r_c) {{")
            parts.append(f'      printf("FAIL test {test_idx}: return mismatch\\\\n");')
            parts.append(f"      n_failed++; goto next_test_{test_idx}; }}")

        # Check buffers
        for name, c_type in sig.params:
            if isinstance(test_input.get(name, 0), list):
                parts.append(f"    for (int __i = 0; __i < MAXN; __i++) {{")
                parts.append(f"      if ({name}_o[__i] != {name}_c[__i]) {{")
                parts.append(f'        printf("FAIL test {test_idx}: buffer {name} mismatch at %d\\\\n", __i);')
                parts.append(f"        n_failed++; goto next_test_{test_idx}; }}}}")

        parts.append(f"    next_test_{test_idx}: ;")
        parts.append(f"  }}")

    parts.append(f'  printf("%d/%d tests passed\\\\n", n_tests - n_failed, n_tests);')
    parts.append(f"  return n_failed > 0 ? 1 : 0;")
    parts.append("}")

    return "\n".join(parts)


def _compile_and_run(
    driver_path: str, task: Task, timeout_s: float, extra_flags: list[str] | None = None
) -> TestResult:
    """Compile the test driver and run it."""
    # Compile
    try:
        result = subprocess.run(
            ["gcc", "-O0", "-fsigned-char", *(extra_flags or []),
             "-o", driver_path + ".out", driver_path],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return TestResult(passed=False, details="Compilation timed out")

    if result.returncode != 0:
        return TestResult(
            passed=False,
            details=f"Compilation error: {result.stderr[:300]}",
        )

    # Run
    try:
        result = subprocess.run(
            [driver_path + ".out"],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return TestResult(passed=False, details="Test execution timed out")
    finally:
        try:
            os.unlink(driver_path + ".out")
        except OSError:
            pass

    stdout = result.stdout
    if result.returncode == 0:
        # Parse "N/total tests passed"
        match = __import__("re").search(r'(\d+)/(\d+) tests passed', stdout)
        if match:
            return TestResult(passed=True, n_tests=int(match.group(2)))
        return TestResult(passed=True, details=stdout[:200])

    return TestResult(passed=False, details=stdout[:500])

# --------------------------------------------------------------------------
# Large-input differential tests (Stage 0.5 P4)
# --------------------------------------------------------------------------

_UB_MEMO: dict[tuple, set[int] | None] = {}


@dataclass
class LargeTestResult:
    verdict: str                     # "passed" | "failed" | "error" | "not_applicable"
    n_tests: int = 0                 # inputs actually compared
    n_dropped_ub: int = 0            # inputs dropped: original has UB on them
    time_s: float = 0.0
    details: str = ""


def _has_length_like_input(task: Task) -> bool:
    """True if input size can grow past MAXN: a pointer or a paired length param."""
    return any(t.strip().endswith("*") for _, t in task.signature.params)


def _original_ub_inputs(task: Task, tests: list[dict], bench_n: int, timeout_s: float) -> set[int] | None:
    """Indices of tests on which the ORIGINAL has UB (UBSan) or crashes. None if UBSan is unavailable."""
    import re as _re
    src = _build_test_driver(task, task.func_src, tests, bench_n, orig_only=True)
    with tempfile.NamedTemporaryFile(suffix=".c", mode="w", prefix="ub_", delete=False) as f:
        f.write(src)
        path = f.name
    exe = path + ".out"
    try:
        comp = subprocess.run(
            ["gcc", "-O0", "-fsigned-char", "-fsanitize=undefined", "-fno-sanitize-recover=all",
             "-o", exe, path],
            capture_output=True, text=True, timeout=30,
        )
        if comp.returncode != 0:
            return None
        run = subprocess.run([exe], capture_output=True, text=True, timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError):
        return None
    finally:
        for p in (path, exe):
            try:
                os.unlink(p)
            except OSError:
                pass
    if run.returncode != 0:
        return None  # the parent itself failed; cannot attribute
    return {int(m.group(1)) for m in _re.finditer(r"^@UB (\d+)$", run.stdout, _re.M)}


def run_large_tests(
    task: Task,
    candidate_src: str,
    *,
    maxn: int = 8,
    bench_n: int = 512,
    n_random: int = 64,
    seed: int = 0,
    timeout_s: float = 60,
) -> LargeTestResult:
    """Differential tests on inputs LARGER than the CBMC bound.

    CBMC proves equivalence only for lengths <= MAXN. These tests draw paired
    lengths and string lengths from (MAXN, bench_n], buffer values from the
    task's input_ranges, and compare candidate against original. Inputs on which
    the original itself has undefined behavior (UBSan) are dropped first, so a
    legitimate candidate is not flagged for differing where the original is
    undefined. Drivers are compiled with -fwrapv for the same reason.
    """
    start = time.monotonic()
    if not _has_length_like_input(task):
        return LargeTestResult(verdict="not_applicable", details="no buffer or length inputs")

    rng = random.Random(seed)
    tests = [
        _generate_random_input(task.signature, task.pointer_rules or {}, bench_n,
                               task.input_ranges, rng=rng, min_len=maxn + 1)
        for _ in range(n_random)
    ]

    # The UB filter depends only on the original and the (seeded) inputs, so
    # memoize it per task: it is half the cost of a call.
    key = (task.task_id, hashlib.sha256(task.func_src.encode()).hexdigest(), bench_n, n_random, seed, maxn)
    if key not in _UB_MEMO:
        _UB_MEMO[key] = _original_ub_inputs(task, tests, bench_n, timeout_s)
    ub = _UB_MEMO[key]
    n_dropped = 0
    if ub:
        n_dropped = len(ub)
        tests = [t for i, t in enumerate(tests) if i not in ub]
    if not tests:
        return LargeTestResult(verdict="error", n_dropped_ub=n_dropped,
                               time_s=time.monotonic() - start,
                               details="original has UB on every large input")

    driver = _build_test_driver(task, candidate_src, tests, bench_n)
    with tempfile.NamedTemporaryFile(suffix=".c", mode="w", prefix="large_", delete=False) as f:
        f.write(driver)
        path = f.name
    try:
        r = _compile_and_run(path, task, timeout_s, extra_flags=["-fwrapv"])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    note = "" if ub is not None else "UBSan unavailable; no UB filtering. "
    elapsed = time.monotonic() - start
    if r.passed:
        return LargeTestResult(verdict="passed", n_tests=len(tests), n_dropped_ub=n_dropped,
                               time_s=elapsed, details=note.strip())
    # Inputs where the original has UB or crashes were dropped above, so a
    # mismatch, crash or timeout now is the candidate's. Only a compile error
    # (or missing UBSan) leaves the verdict undecided.
    if "FAIL test" in r.details or (ub is not None and not r.details.startswith("Compilation")):
        return LargeTestResult(verdict="failed", n_tests=len(tests), n_dropped_ub=n_dropped,
                               time_s=elapsed, details=note + r.details[:300])
    return LargeTestResult(verdict="error", n_tests=len(tests), n_dropped_ub=n_dropped,
                           time_s=elapsed, details=note + r.details[:300])
