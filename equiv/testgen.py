"""
testgen.py — Cheap IO/random test pre-filter for candidate functions.

Runs the original and candidate on test inputs (ExeBench IO pairs + random +
edge cases) and checks output equality. Most wrong candidates die here
in milliseconds, before the expensive CBMC check.

Usage:
    from equiv.testgen import run_tests, TestResult
"""

from __future__ import annotations

import random
import subprocess
import tempfile
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .extract import Task
from .harness import generate_harness, NONDET_DECLS, HEADER


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
    for _ in range(n_random):
        test_input = _generate_random_input(sig, task.pointer_rules, maxn, task.input_ranges)
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
    sig, pointer_rules: dict, maxn: int, input_ranges: dict | None = None
) -> dict:
    """Generate one set of random inputs matching the function signature,
    respecting paired length bounds [0, maxn] and task.input_ranges."""
    from .extract import get_paired_length_params
    from .harness import _type_bounds

    paired_lengths = get_paired_length_params(sig, pointer_rules)
    ranges = input_ranges or {}
    narrowed = bool(ranges.get("_narrowed"))
    buf_range = ranges.get("_buf_range", (-32768, 32767))

    inp = {}
    for name, c_type in sig.params:
        if c_type.strip().endswith("*"):
            pointee = c_type.rstrip("*").replace("const ", "").strip()
            if "char" in pointee and pointer_rules.get(name) == "string":
                length = random.randint(0, maxn - 1)
                arr = [random.randint(32, 126) for _ in range(length)] + [0]
                arr = arr[:maxn]
                while len(arr) < maxn:
                    arr.append(0)
            else:
                if narrowed:
                    tmin, tmax, _ = _type_bounds(pointee)
                    lo = max(int(buf_range[0]), tmin)
                    hi = min(int(buf_range[1]), tmax)
                    arr = [random.randint(lo, hi) for _ in range(maxn)]
                else:
                    arr = _random_scalar_array(pointee, maxn)
            inp[name] = arr
        elif name in paired_lengths:
            inp[name] = random.randint(0, maxn)
        elif name in ranges and isinstance(ranges[name], (list, tuple)) and len(ranges[name]) == 2:
            inp[name] = random.randint(int(ranges[name][0]), int(ranges[name][1]))
        elif narrowed:
            tmin, tmax, _ = _type_bounds(c_type)
            inp[name] = random.randint(max(-32768, tmin), min(32767, tmax))
        else:
            inp[name] = _random_scalar(c_type)
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
            if name in paired_lengths:
                edges = sorted({0, 1, maxn // 2, max(0, maxn - 1), maxn})
            else:
                base_type = c_type.replace("const ", "").strip()
                raw_edges = EDGE_VALUES.get(base_type, [0, 1, -1])
                if name in ranges and isinstance(ranges[name], (list, tuple)) and len(ranges[name]) == 2:
                    lo, hi = int(ranges[name][0]), int(ranges[name][1])
                    edges = [v for v in raw_edges if lo <= v <= hi] + [lo, hi]
                elif narrowed:
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
            pointee = c_type.rstrip("*").replace("const ", "").strip()
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


def _random_scalar(c_type: str) -> int:
    """Generate a random value for a scalar type."""
    base = c_type.replace("const ", "").strip()
    if base in ("char", "unsigned char", "int8_t", "uint8_t", "bool", "_Bool"):
        return random.randint(-128, 127)
    elif base in ("short", "unsigned short", "int16_t", "uint16_t"):
        return random.randint(-32768, 32767)
    elif base in ("size_t", "unsigned int", "uint32_t"):
        return random.randint(0, 1000000)
    else:
        return random.randint(-1000000, 1000000)


def _random_scalar_array(pointee_type: str, maxn: int) -> list[int]:
    """Generate a random array of scalar values."""
    if pointee_type in ("char", "unsigned char"):
        return [random.randint(-128, 127) for _ in range(maxn)]
    elif pointee_type in ("short", "unsigned short"):
        return [random.randint(-32768, 32767) for _ in range(maxn)]
    else:
        return [random.randint(-1000000, 1000000) for _ in range(maxn)]


def _build_test_driver(
    task: Task, candidate_src: str, tests: list[dict], maxn: int
) -> str:
    """Build a C test driver that runs original and candidate on each test case."""
    parts = []

    parts.append(HEADER)
    parts.append(f"#define MAXN {maxn}")

    # Include deps (filtered)
    from .harness import _filter_deps
    if task.deps_src:
        filtered = _filter_deps(task.deps_src)
        if filtered:
            parts.append(filtered)

    # Shorthand typedefs for scalar types used in tests
    parts.append("""typedef long long int64_t;
typedef unsigned long long uint64_t;
typedef long intptr_t;
typedef unsigned long uintptr_t;""")

    # Renamed original and candidate
    from .harness import _rename_function, _extract_function_name
    orig = _rename_function(task.func_src, task.func_name, f"orig__{task.func_name}")
    if orig is None:
        orig = task.func_src
    parts.append(orig)

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

int main(void) {{
    int n_failed = 0;
    int n_tests = {len(tests)};
""")

    sig = task.signature
    for test_idx, test_input in enumerate(tests):
        parts.append(f"  {{ /* Test {test_idx} */")

        # Declare and initialize inputs
        for name, c_type in sig.params:
            val = test_input.get(name, 0)
            if isinstance(val, list):
                pointee = c_type.rstrip("*").replace("const ", "").strip()
                parts.append(f"    {pointee} {name}_o[MAXN];")
                parts.append(f"    {pointee} {name}_c[MAXN];")
                for j, v in enumerate(val):
                    parts.append(f"    {name}_o[{j}] = {v};")
                    parts.append(f"    {name}_c[{j}] = {v};")
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
    driver_path: str, task: Task, timeout_s: float
) -> TestResult:
    """Compile the test driver and run it."""
    # Compile
    try:
        result = subprocess.run(
            ["gcc", "-O0", "-fsigned-char", "-o", driver_path + ".out", driver_path],
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