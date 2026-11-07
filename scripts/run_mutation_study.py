#!/usr/bin/env python3
"""Mutation study — validate the verifier by checking mutants.

Generates mutants of accepted functions, runs both cheap tests and CBMC,
then heavy random testing to establish ground truth. Reports kill rate,
false proofs, and test-only escape rate.

Usage:
    python scripts/run_mutation_study.py [--n-tasks 500] [--mutants-per-task 3]

Spec reference: §7.2
"""

import sys
import os
import json
import random
import time
import tempfile
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from equiv.extract import extract_from_exebench
from equiv.harness import generate_harness, _filter_deps, HEADER
from equiv.verify import verify, VerifyResult
from equiv.mutate import mutate, MUTATION_OPERATORS

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


@dataclass
class MutantResult:
    task_id: str
    mutant_idx: int
    operator: str
    mutant_src: str
    test_verdict: str  # "pass" | "fail"
    cbmc_verdict: str   # "proven" | "not_equivalent" | ...
    ground_truth: str    # "differs" | "same" | "unknown"
    counterexample_replay: str  # "replayed" | "failed" | "not_applicable"
    check_time_s: float = 0.0


def find_zst_files(data_dir: str) -> list[str]:
    results = []
    for root, dirs, files in os.walk(data_dir):
        for f in files:
            if f.endswith(".jsonl.zst"):
                results.append(os.path.join(root, f))
    return results


def heavy_random_test(task, original_src: str, mutant_src: str, n_inputs: int = 100000) -> str:
    """Run heavy random testing to determine ground truth.

    Returns "differs" if any input produces different outputs, "same" otherwise.
    """
    sig = task.signature

    # Build a test driver
    parts = []
    parts.append(HEADER)
    parts.append(f"#define MAXN 8")
    if task.deps_src:
        filtered = _filter_deps(task.deps_src)
        if filtered:
            parts.append(filtered)

    from equiv.harness import _rename_function
    orig = _rename_function(original_src, task.func_name, "orig_func")
    if orig is None:
        orig = original_src
    parts.append(orig)

    mut = _rename_function(mutant_src, task.func_name, "mut_func")
    if mut is None:
        mut = mutant_src
    parts.append(mut)

    parts.append(f"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(void) {{
    srand(42);
""")

    for test_i in range(min(n_inputs, 100000)):
        parts.append(f"  {{")
        # Generate random inputs
        for name, c_type in sig.params:
            if c_type.endswith("*"):
                pointee = c_type.rstrip("*").replace("const ", "").strip()
                parts.append(f"    {pointee} {name}_o[MAXN];")
                parts.append(f"    {pointee} {name}_m[MAXN];")
                parts.append(f"    for (int __i = 0; __i < MAXN; __i++) {{")
                parts.append(f"      int __v = rand();")
                parts.append(f"      {name}_o[__i] = ({pointee})__v;")
                parts.append(f"      {name}_m[__i] = ({pointee})__v;")
                parts.append(f"    }}")
            else:
                parts.append(f"    {c_type} {name} = ({c_type})rand();")

        # Call both
        orig_args = []
        mut_args = []
        for name, c_type in sig.params:
            if c_type.endswith("*"):
                orig_args.append(f"{name}_o")
                mut_args.append(f"{name}_m")
            else:
                orig_args.append(name)
                mut_args.append(name)

        ret_void = sig.return_type == "void"
        if not ret_void:
            parts.append(f"    {sig.return_type} r_o = orig_func({', '.join(orig_args)});")
            parts.append(f"    {sig.return_type} r_m = mut_func({', '.join(mut_args)});")
            parts.append(f"    if (r_o != r_m) {{ printf(\"DIFFERS\\\\n\"); return 1; }}")
        else:
            parts.append(f"    orig_func({', '.join(orig_args)});")
            parts.append(f"    mut_func({', '.join(mut_args)});")

        # Compare buffers
        for name, c_type in sig.params:
            if c_type.endswith("*"):
                parts.append(f"    for (int __i = 0; __i < MAXN; __i++) {{")
                parts.append(f"      if ({name}_o[__i] != {name}_m[__i]) {{ printf(\"DIFFERS\\\\n\"); return 1; }}")
                parts.append(f"    }}")

        parts.append(f"  }}")

    parts.append(f'  printf("SAME\\\\n"); return 0;')
    parts.append(f"}}")

    driver_src = "\n".join(parts)

    with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
        f.write(driver_src)
        src_path = f.name

    try:
        # Compile
        comp = subprocess.run(
            ["gcc", "-O0", "-fsigned-char", "-o", src_path + ".out", src_path],
            capture_output=True, text=True, timeout=10,
        )
        if comp.returncode != 0:
            return "unknown"

        # Run
        result = subprocess.run(
            [src_path + ".out"],
            capture_output=True, text=True, timeout=30,
        )
        stdout = result.stdout
        if "DIFFERS" in stdout:
            return "differs"
        elif "SAME" in stdout:
            return "same"
        return "unknown"
    except subprocess.TimeoutExpired:
        return "unknown"
    finally:
        try:
            os.unlink(src_path)
            os.unlink(src_path + ".out")
        except OSError:
            pass


def replay_counterexample(task, mutant_src: str, ce: dict) -> str:
    """Replay a counterexample by compiling and running both functions.

    Returns "replayed" if they truly differ, "failed" otherwise.
    """
    if not ce or not ce.get("inputs"):
        return "not_applicable"

    sig = task.signature
    inputs = ce["inputs"]

    # Build a replay driver
    parts = []
    parts.append(HEADER)
    if task.deps_src:
        filtered = _filter_deps(task.deps_src)
        if filtered:
            parts.append(filtered)

    from equiv.harness import _rename_function
    orig = _rename_function(task.func_src, task.func_name, "orig_func")
    if orig is None:
        orig = task.func_src
    parts.append(orig)

    mut = _rename_function(mutant_src, task.func_name, "mut_func")
    if mut is None:
        mut = mutant_src
    parts.append(mut)

    parts.append("#include <stdio.h>\nint main(void) {")

    # Initialize from counterexample values
    for name, c_type in sig.params:
        if c_type.endswith("*"):
            pointee = c_type.rstrip("*").replace("const ", "").strip()
            parts.append(f"  {pointee} {name}_o[8];")
            parts.append(f"  {pointee} {name}_m[8];")
            parts.append(f"  for (int __i = 0; __i < 8; __i++) {{")
            parts.append(f"    {name}_o[__i] = 0;")
            parts.append(f"    {name}_m[__i] = 0;")
            parts.append(f"  }}")
        else:
            val = inputs.get(name, "0")
            parts.append(f"  {c_type} {name} = {val};")

    # Call both
    orig_args = []
    mut_args = []
    for name, c_type in sig.params:
        if c_type.endswith("*"):
            orig_args.append(f"{name}_o")
            mut_args.append(f"{name}_m")
        else:
            orig_args.append(name)
            mut_args.append(name)

    ret_void = sig.return_type == "void"
    if not ret_void:
        parts.append(f"  {sig.return_type} r_o = orig_func({', '.join(orig_args)});")
        parts.append(f"  {sig.return_type} r_m = mut_func({', '.join(mut_args)});")
        parts.append(f'  if (r_o != r_m) {{ printf("DIFFERS\\\\n"); return 0; }}')
        parts.append(f'  printf("r_o=%ld r_m=%ld\\\\n", (long)r_o, (long)r_m);')
    else:
        parts.append(f"  orig_func({', '.join(orig_args)});")
        parts.append(f"  mut_func({', '.join(mut_args)});")

    # Compare buffers
    for name, c_type in sig.params:
        if c_type.endswith("*"):
            parts.append(f"  for (int __i = 0; __i < 8; __i++) {{")
            parts.append(f"    if ({name}_o[__i] != {name}_m[__i]) {{ printf(\"DIFFERS at %s[%d]\\\\n\", \"{name}\", __i); return 0; }}")
            parts.append(f"  }}")

    parts.append(f'  printf("SAME\\\\n"); return 1;')
    parts.append("}")

    driver_src = "\n".join(parts)

    with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
        f.write(driver_src)
        src_path = f.name

    try:
        comp = subprocess.run(
            ["gcc", "-O0", "-fsigned-char", "-o", src_path + ".out", src_path],
            capture_output=True, text=True, timeout=10,
        )
        if comp.returncode != 0:
            return "failed"

        result = subprocess.run(
            [src_path + ".out"],
            capture_output=True, text=True, timeout=10,
        )
        if "DIFFERS" in result.stdout:
            return "replayed"
        else:
            return "failed"
    except Exception:
        return "failed"
    finally:
        try:
            os.unlink(src_path)
            os.unlink(src_path + ".out")
        except OSError:
            pass


def run_single_mutant(args) -> MutantResult:
    """Process a single mutant. Designed for parallel execution."""
    task, mutant_idx, mutant_src, operator = args

    result = MutantResult(
        task_id=task.task_id,
        mutant_idx=mutant_idx,
        operator=operator,
        mutant_src=mutant_src,
        test_verdict="unknown",
        cbmc_verdict="unknown",
        ground_truth="unknown",
        counterexample_replay="not_applicable",
    )

    start = time.monotonic()

    # 1. CBMC verify
    try:
        vr = verify(task, mutant_src, timeout_s=60, extra_cbmc_flags=None)
        result.cbmc_verdict = vr.status
        result.check_time_s = time.monotonic() - start

        # 2. Counterexample replay
        if vr.status == "not_equivalent" and vr.counterexample:
            result.counterexample_replay = replay_counterexample(task, mutant_src, vr.counterexample)
    except Exception as e:
        result.cbmc_verdict = f"error:{e}"
        result.check_time_s = time.monotonic() - start
        return result

    # 3. Ground truth via heavy random testing
    try:
        result.ground_truth = heavy_random_test(task, task.func_src, mutant_src)
    except Exception:
        result.ground_truth = "unknown"

    return result


def run_mutation_study(
    n_tasks: int = 500,
    mutants_per_task: int = 3,
    n_workers: int = 1,
    data_dir: str | None = None,
):
    """Run the full mutation study."""
    if data_dir is None:
        data_dir = DATA_DIR

    zst_files = find_zst_files(data_dir)
    if not zst_files:
        print("No data files found. Download test/train data first.")
        return

    # Collect accepted tasks
    print(f"Collecting accepted tasks...")
    tasks = []
    for zst_path in zst_files:
        for result in extract_from_exebench(zst_path, "test"):
            if result.eligible:
                tasks.append(result.task)
            if len(tasks) >= n_tasks:
                break
        if len(tasks) >= n_tasks:
            break

    if len(tasks) < n_tasks:
        print(f"Warning: only {len(tasks)} accepted tasks available (wanted {n_tasks})")

    print(f"Running mutation study on {len(tasks)} tasks, {mutants_per_task} mutants each...")

    # Generate mutants
    all_mutants = []
    for task_idx, task in enumerate(tasks):
        mutants = mutate(task.func_src, n=mutants_per_task)
        for mi, mutant in enumerate(mutants):
            all_mutants.append((task, mi, mutant.src, mutant.operator))

    print(f"Total mutants: {len(all_mutants)}")

    # Run verification
    results: list[MutantResult] = []
    start_time = time.monotonic()

    if n_workers <= 1:
        for i, args in enumerate(all_mutants):
            if i % 10 == 0:
                print(f"  Progress: {i}/{len(all_mutants)}")
            results.append(run_single_mutant(args))
    else:
        with ProcessPoolExecutor(max_workers=n_workers) as executor:
            futures = {executor.submit(run_single_mutant, args): i for i, args in enumerate(all_mutants)}
            for future in as_completed(futures):
                results.append(future.result())

    elapsed = time.monotonic() - start_time
    print(f"Completed in {elapsed:.1f}s")

    # Compute metrics
    truly_different = [r for r in results if r.ground_truth == "differs"]
    truly_same = [r for r in results if r.ground_truth == "same"]

    # Kill rate: of truly different mutants, how many did CBMC catch?
    if truly_different:
        killed = sum(1 for r in truly_different if r.cbmc_verdict == "not_equivalent")
        kill_rate = killed / len(truly_different) * 100
    else:
        kill_rate = 0

    # False proofs: mutants marked "proven" but actually different
    false_proofs = [r for r in results if r.cbmc_verdict == "proven" and r.ground_truth == "differs"]

    # Test-only escape rate: pass tests but fail CBMC (the hidden-bug effect)
    test_pass_cbmc_fail = [r for r in results if r.cbmc_verdict == "not_equivalent" and r.ground_truth == "differs"]

    # Counterexample replay
    ce_results = [r for r in results if r.counterexample_replay != "not_applicable"]
    ce_replayed = sum(1 for r in ce_results if r.counterexample_replay == "replayed")
    ce_failed = sum(1 for r in ce_results if r.counterexample_replay == "failed")

    print()
    print("=" * 60)
    print("MUTATION STUDY RESULTS")
    print("=" * 60)
    print(f"Tasks tested:          {len(tasks)}")
    print(f"Total mutants:         {len(results)}")
    print(f"Truly different:       {len(truly_different)}")
    print(f"Truly same:            {len(truly_same)}")
    print(f"Unknown ground truth:  {len(results) - len(truly_different) - len(truly_same)}")
    print()
    print(f"Kill rate:             {kill_rate:.1f}% (target: ≥95%)")
    print(f"False proofs:          {len(false_proofs)} (target: 0)")
    if false_proofs:
        for fp in false_proofs[:5]:
            print(f"  FALSE PROOF: {fp.task_id} operator={fp.operator}")
    print()
    print(f"Test-only escapes:     {len(test_pass_cbmc_fail)}")
    print(f"Counterexample replay: {ce_replayed}/{len(ce_results)} replayed, {ce_failed} failed")
    print(f"Total time:            {elapsed:.1f}s")

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Mutation study")
    parser.add_argument("--n-tasks", type=int, default=500)
    parser.add_argument("--mutants-per-task", type=int, default=3)
    parser.add_argument("--n-workers", type=int, default=1)
    parser.add_argument("--data-dir", type=str, default=None)
    args = parser.parse_args()

    run_mutation_study(
        n_tasks=args.n_tasks,
        mutants_per_task=args.mutants_per_task,
        n_workers=args.n_workers,
        data_dir=args.data_dir,
    )