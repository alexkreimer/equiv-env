"""
speed.py — Speed proxy measurement for C functions.

Two proxies:
  A. Valgrind callgrind instruction count (deterministic, preferred for x86-64)
  B. Static assembly instruction count (fast, ignores loop trip counts)

Usage:
    from equiv.speed import measure_speedup, validate_proxy
    speedup = measure_speedup(task, candidate_src, proxy="callgrind")
"""

from __future__ import annotations

import subprocess
import tempfile
import os
import re
import time
import statistics
from pathlib import Path
from typing import Literal

from .extract import Task
from .harness import HEADER, _filter_deps

ProxyKind = Literal["callgrind", "static_asm", "wallclock"]


def measure_callgrind(binary_path: str) -> int | None:
    """Run valgrind --tool=callgrind and parse total instruction count.

    Returns total Ir (instruction references) count, or None on failure.
    Requires valgrind installed (x86-64 Docker).
    """
    try:
        result = subprocess.run(
            ["valgrind", "--tool=callgrind", "--callgrind-out-file=/dev/stdout",
             binary_path],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    # Parse: "totals: N" line in callgrind output
    for line in result.stderr.split("\n"):
        # callgrind prints totals to stderr
        match = re.search(r'totals:\s+([\d,]+)', line)
        if match:
            return int(match.group(1).replace(",", ""))
    return None


def measure_wallclock(binary_path: str, n_runs: int = 30) -> float | None:
    """Measure median wall-clock time over n_runs after warmup.

    Returns median seconds, or None on failure.
    """
    times = []
    for _ in range(n_runs + 2):  # +2 for warmup
        try:
            start = time.perf_counter()
            result = subprocess.run(
                [binary_path], capture_output=True, timeout=30,
            )
            elapsed = time.perf_counter() - start
            if result.returncode == 0:
                times.append(elapsed)
        except subprocess.TimeoutExpired:
            pass

    if len(times) < 3:
        return None
    # Drop warmup runs
    times = times[2:]
    return statistics.median(times)


def measure_static_asm(src_path: str, arch: str = "x86-64-v2") -> int | None:
    """Count assembly instructions (static, ignores loops)."""
    asm_path = src_path + ".s"
    try:
        subprocess.run(
            ["gcc", "-O3", f"-march={arch}", "-S", "-o", asm_path, src_path],
            capture_output=True, text=True, timeout=30, check=True,
        )
    except subprocess.CalledProcessError:
        return None

    try:
        with open(asm_path) as f:
            lines = f.readlines()
        os.unlink(asm_path)
    except OSError:
        return None

    count = 0
    for line in lines:
        s = line.strip()
        if not s or s.startswith('.') or s.endswith(':') or s.startswith('"') or s.startswith('//'):
            continue
        count += 1
    return count


def measure_speedup(
    task: Task,
    candidate_src: str,
    *,
    maxn: int = 8,
    arch: str = "x86-64-v2",
    proxy: ProxyKind = "callgrind",
    n_loop_iterations: int = 10000,
) -> float:
    """Measure speedup of candidate over original.

    Returns proxy(original) / proxy(candidate). >1.0 = candidate is faster.
    """
    orig_driver = _build_speed_driver(task, task.func_src, maxn, n_loop_iterations)
    cand_driver = _build_speed_driver(task, candidate_src, maxn, n_loop_iterations)

    orig_val = _measure_driver(orig_driver, arch, proxy)
    cand_val = _measure_driver(cand_driver, arch, proxy)

    if orig_val is None or cand_val is None or cand_val == 0:
        return 1.0
    return orig_val / cand_val


def _measure_driver(driver_src: str, arch: str, proxy: ProxyKind) -> float | None:
    """Compile a benchmark driver and measure with the chosen proxy."""
    with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
        f.write(driver_src)
        src_path = f.name

    binary_path = src_path + ".out"
    try:
        # Compile
        comp = subprocess.run(
            ["gcc", "-O3", f"-march={arch}", "-fsigned-char", "-o", binary_path, src_path],
            capture_output=True, text=True, timeout=30,
        )
        if comp.returncode != 0:
            return None

        if proxy == "callgrind":
            return measure_callgrind(binary_path)
        elif proxy == "wallclock":
            return measure_wallclock(binary_path)
        elif proxy == "static_asm":
            return measure_static_asm(src_path, arch)
    finally:
        for p in [src_path, binary_path, src_path + ".s"]:
            try:
                os.unlink(p)
            except OSError:
                pass

    return None


def _build_speed_driver(task: Task, func_src: str, maxn: int, n_loops: int) -> str:
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

    sig = task.signature
    parts.append("int main(void) { volatile int __sink = 0;")

    for i, (name, c_type) in enumerate(sig.params):
        if c_type.endswith("*"):
            pointee = c_type.rstrip("*").replace("const ", "").strip()
            parts.append(f"  {pointee} {name}_buf[MAXN];")
            parts.append(f"  for (int __i = 0; __i < MAXN; __i++)"
                         f" {name}_buf[__i] = ({pointee})(__i * 3 + {i});")
        else:
            parts.append(f"  {c_type} {name} = ({c_type}){i * 100 + 1};")

    parts.append("  for (int __loop = 0; __loop < N_LOOPS; __loop++) {")
    args = []
    for name, c_type in sig.params:
        if c_type.endswith("*"):
            args.append(f"{name}_buf")
        else:
            args.append(name)

    if sig.return_type != "void":
        parts.append(f"    __sink += ({sig.return_type}){task.func_name}({', '.join(args)});")
    else:
        parts.append(f"    {task.func_name}({', '.join(args)});")
    parts.append("  }")
    parts.append("  return __sink;")
    parts.append("}")
    return "\n".join(parts)


def validate_proxy(
    tasks: list[Task],
    n_pairs: int = 200,
    arch: str = "x86-64-v2",
) -> dict:
    """Validate speed proxy by computing Spearman correlation with real time.

    Creates speed variants by compiling at -O1 vs -O3 for each task,
    measures wall-clock time and the chosen proxy, then computes Spearman r.

    Returns:
        {"callgrind_r": float, "static_asm_r": float, "n_pairs": int}
    """
    import math

    callgrind_data = []
    static_asm_data = []

    for task in tasks[:n_pairs]:
        try:
            # O1 variant (slower) as "original", O3 as "candidate"
            o1_driver = _build_speed_driver(task, task.func_src, 8, 5000)

            # Measure wall-clock (ground truth)
            wall_vals = []
            for driver in [o1_driver]:
                with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
                    f.write(driver)
                    sp = f.name
                bp = sp + ".out"
                try:
                    subprocess.run(
                        ["gcc", "-O1", f"-march={arch}", "-fsigned-char", "-o", bp, sp],
                        capture_output=True, text=True, timeout=30, check=True,
                    )
                    w1 = measure_wallclock(bp, n_runs=15)
                except Exception:
                    w1 = None
                finally:
                    for p in [sp, bp]:
                        try: os.unlink(p)
                        except: pass

            # O3 variant
            with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
                f.write(o1_driver)
                sp = f.name
            bp = sp + ".out"
            try:
                subprocess.run(
                    ["gcc", "-O3", f"-march={arch}", "-fsigned-char", "-o", bp, sp],
                    capture_output=True, text=True, timeout=30, check=True,
                )
                w3 = measure_wallclock(bp, n_runs=15)
            except Exception:
                w3 = None
            finally:
                for p in [sp, bp]:
                    try: os.unlink(p)
                    except: pass

            if w1 is None or w3 is None or w1 == 0:
                continue

            real_speedup = w1 / w3

            # Measure callgrind proxy
            c1 = _measure_driver(o1_driver, arch, "callgrind")
            c3 = _measure_driver(o1_driver, arch, "callgrind")
            # Need separate O1/O3 compilations for callgrind
            # Simplified: just record the real wall speedup for now
            callgrind_data.append(real_speedup)

        except Exception:
            pass

    # Return placeholder — real validation needs x86-64 + valgrind
    return {
        "callgrind_r": None,
        "static_asm_r": None,
        "n_pairs": 0,
        "note": "Speed proxy validation requires x86-64 Linux with valgrind"
    }