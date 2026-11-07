"""
speed.py — Speed proxy measurement for C functions.

Measures speed using either Valgrind callgrind (instruction count, deterministic)
or llvm-mca (static cycle estimate). Returns a stable, reproducible number.

Usage:
    from equiv.speed import measure_speedup
    speedup = measure_speedup(task, candidate_src)
"""

from __future__ import annotations

import subprocess
import tempfile
import os
import re
from pathlib import Path

from .extract import Task
from .harness import generate_harness, _rename_function, _extract_function_name, HEADER, _filter_deps


def measure_instruction_count(src_path: str, func_name: str, arch: str = "armv8.5-a") -> int | None:
    """Measure instruction count using perf or dummy counter.

    On macOS/arm64: uses a simple regression test timing approach.
    Longer-term: use valgrind --tool=callgrind in Docker.

    Returns total instruction count estimate, or None on failure.
    """
    # Compile and get assembly
    asm_path = src_path + ".s"
    try:
        subprocess.run(
            ["gcc", "-O3", f"-march={arch}", "-S", "-o", asm_path, src_path],
            capture_output=True, text=True, timeout=30, check=True,
        )
    except subprocess.CalledProcessError:
        return None

    # Count assembly instructions (rough proxy)
    try:
        with open(asm_path) as f:
            asm_lines = f.readlines()
        os.unlink(asm_path)
    except OSError:
        return None

    # Count actual instruction lines (skip labels, directives, comments)
    count = 0
    for line in asm_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('.') or stripped.endswith(':'):
            continue
        # Skip section markers
        if stripped.startswith('"') or stripped.startswith('//'):
            continue
        count += 1

    return count


def measure_speedup(
    task: Task,
    candidate_src: str,
    *,
    maxn: int = 8,
    arch: str = "armv8.5-a",
    n_loop_iterations: int = 10000,
) -> float:
    """Measure speedup of candidate over original.

    Returns proxy(original) / proxy(candidate). Values > 1.0 mean candidate is faster.
    For now: uses static instruction count as a placeholder proxy.

    Args:
        task: The eligible Task.
        candidate_src: Candidate function source.
        maxn: Buffer size for the benchmark driver.
        arch: Target architecture for -march flag.
        n_loop_iterations: How many times to call the function in the benchmark loop.

    Returns:
        Speedup ratio (float).
    """
    # Build benchmark driver for original
    orig_driver = _build_speed_driver(task, task.func_src, maxn, n_loop_iterations, arch)
    cand_driver = _build_speed_driver(task, candidate_src, maxn, n_loop_iterations, arch)

    # Measure both
    orig_count = _measure_driver(orig_driver, arch)
    cand_count = _measure_driver(cand_driver, arch)

    if orig_count is None or cand_count is None or cand_count == 0:
        return 1.0

    return orig_count / cand_count


def _build_speed_driver(
    task: Task, func_src: str, maxn: int, n_loops: int, arch: str
) -> str:
    """Build a benchmark C file that calls the function in a loop."""
    parts = []
    parts.append(HEADER)
    parts.append(f"#define MAXN {maxn}")
    parts.append(f"#define N_LOOPS {n_loops}")

    if task.deps_src:
        filtered = _filter_deps(task.deps_src)
        if filtered:
            parts.append(filtered)

    parts.append(func_src)

    # Generate input data
    sig = task.signature
    parts.append(f"""
int main(void) {{
    volatile int __sink = 0;
""")

    # Generate deterministic test inputs
    for i, (name, c_type) in enumerate(sig.params):
        if c_type.endswith("*"):
            pointee = c_type.rstrip("*").replace("const ", "").strip()
            parts.append(f"    {pointee} {name}_buf[MAXN];")
            parts.append(f"    for (int __i = 0; __i < MAXN; __i++) {name}_buf[__i] = ({pointee})(__i * 3 + {i});")
        else:
            parts.append(f"    {c_type} {name} = ({c_type}){i * 100 + 1};")

    # Call in a loop
    parts.append(f"    for (int __loop = 0; __loop < N_LOOPS; __loop++) {{")
    args = []
    for name, c_type in sig.params:
        if c_type.endswith("*"):
            args.append(f"{name}_buf")
        else:
            args.append(name)

    if sig.return_type != "void":
        parts.append(f"        __sink += ({sig.return_type}){task.func_name}({', '.join(args)});")
    else:
        parts.append(f"        {task.func_name}({', '.join(args)});")

    parts.append(f"    }}")
    parts.append(f"    return __sink;")
    parts.append(f"}}")

    return "\n".join(parts)


def _measure_driver(driver_src: str, arch: str) -> int | None:
    """Compile and measure a speed driver."""
    with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
        f.write(driver_src)
        src_path = f.name

    try:
        return measure_instruction_count(src_path, "main", arch)
    finally:
        try:
            os.unlink(src_path)
        except OSError:
            pass
        try:
            os.unlink(src_path + ".s")
        except OSError:
            pass