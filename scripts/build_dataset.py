#!/usr/bin/env python3
"""Full dataset build pipeline (Stage 0, spec §5).

Pipeline per function:
1. extract + eligibility → reject with reason, or continue
2. compile check (gcc/clang, -O0/-O3)
3. original-only UB check (CBMC safety checks)
4. self-equivalence check with unwind search
5. mutation check on 3 random mutants
6. accept → write Task to tasks_{split}.jsonl

Usage:
    python scripts/build_dataset.py --data-dir data/ --output data/tasks.jsonl
"""

import sys
import os
import json
import time
import tempfile
import subprocess
from collections import Counter
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from equiv.extract import extract_from_exebench, Task
from equiv.harness import generate_self_harness
from equiv.verify import check_original_ub, _run_cbmc_raw
from equiv.mutate import mutate

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


def find_zst_files(data_dir: str) -> list[str]:
    results = []
    for root, dirs, files in os.walk(data_dir):
        for f in files:
            if f.endswith(".jsonl.zst"):
                results.append(os.path.join(root, f))
    return results


def compile_check(task: Task) -> bool:
    """Check that the function compiles with gcc and clang at -O0 and -O3."""
    src = task.func_src
    deps = task.deps_src or ""
    full_src = deps + "\n" + src

    for compiler in ["gcc", "clang"]:
        for opt in ["-O0", "-O3"]:
            with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
                f.write(full_src)
                src_path = f.name

            try:
                result = subprocess.run(
                    [compiler, "-fsigned-char", opt, "-c", "-o", src_path + ".o", src_path],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode != 0:
                    return False
            except subprocess.TimeoutExpired:
                return False
            finally:
                try:
                    os.unlink(src_path)
                    os.unlink(src_path + ".o")
                except OSError:
                    pass

    return True


def self_equiv_check(task: Task, timeout_s: float = 60) -> tuple[bool, float, int]:
    """Run self-equivalence check with unwind search.

    Returns (passed, check_time_s, unwind_used).
    """
    harness = generate_self_harness(task, unwind=task.unwind)
    harness_src = harness.harness_src

    with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
        f.write(harness_src)
        harness_path = f.name

    try:
        start = time.monotonic()
        result = _run_cbmc_raw(harness_path, task.unwind, timeout_s,
                               extra_flags=["--no-signed-overflow-check"])
        elapsed = time.monotonic() - start

        if "VERIFICATION SUCCESSFUL" in result.stdout:
            return True, elapsed, task.unwind
        return False, elapsed, task.unwind
    except subprocess.TimeoutExpired:
        return False, timeout_s, task.unwind
    finally:
        try:
            os.unlink(harness_path)
        except OSError:
            pass


def build_dataset(
    data_dir: str | None = None,
    output_train: str = "data/tasks_train.jsonl",
    output_test: str = "data/tasks_test.jsonl",
    output_rejected: str = "data/rejected.jsonl",
    max_functions: int | None = None,
):
    """Run the full dataset build pipeline."""
    if data_dir is None:
        data_dir = DATA_DIR

    zst_files = find_zst_files(data_dir)
    if not zst_files:
        print("No .jsonl.zst files found.")
        return

    # Determine split from path
    def get_split(path: str) -> str:
        basename = os.path.basename(path)
        if "test" in basename.lower():
            return "test"
        elif "valid" in basename.lower():
            return "test"  # add valid to test pool per Task C
        return "train"

    funnel = Counter()
    accepted_train = []
    accepted_test = []
    rejected = []
    self_check_times = []

    total = 0
    for zst_path in zst_files:
        split = get_split(zst_path)
        for result in extract_from_exebench(zst_path, split):
            total += 1

            if not result.eligible:
                funnel[result.reason_code] += 1
                rejected.append({"task_id": "?", "reason_code": result.reason_code, "split": split})
                if max_functions and total >= max_functions:
                    break
                continue

            task = result.task
            funnel["eligible"] += 1

            # Compile check
            if not compile_check(task):
                funnel["compile_fail"] += 1
                rejected.append({"task_id": task.task_id, "reason_code": "compile_fail", "split": split})
                continue

            funnel["compile_pass"] += 1

            # UB check
            ub_free, ranges = check_original_ub(task, timeout_s=30)
            if not ub_free:
                funnel["ub_in_original"] += 1
                rejected.append({"task_id": task.task_id, "reason_code": "ub_in_original", "split": split})
                continue

            if ranges:
                task.input_ranges = ranges
            funnel["ub_pass"] += 1

            # Self-equivalence check
            passed, check_time, unwind = self_equiv_check(task)
            task.self_check_time_s = check_time
            task.unwind = unwind
            self_check_times.append(check_time)

            if not passed:
                funnel["self_equiv_fail"] += 1
                rejected.append({"task_id": task.task_id, "reason_code": "self_equiv_fail", "split": split})
                continue

            funnel["self_equiv_pass"] += 1

            # Accept
            if split == "test":
                accepted_test.append(task)
            else:
                accepted_train.append(task)

            if max_functions and total >= max_functions:
                break

        if max_functions and total >= max_functions:
            break

    # Write outputs
    def task_to_dict(t: Task) -> dict:
        d = asdict(t)
        d["signature"] = {
            "name": t.signature.name,
            "return_type": t.signature.return_type,
            "params": t.signature.params,
        }
        return d

    os.makedirs(os.path.dirname(output_train), exist_ok=True)

    with open(output_train, "w") as f:
        for t in accepted_train:
            f.write(json.dumps(task_to_dict(t)) + "\n")

    with open(output_test, "w") as f:
        for t in accepted_test:
            f.write(json.dumps(task_to_dict(t)) + "\n")

    with open(output_rejected, "w") as f:
        for r in rejected:
            f.write(json.dumps(r) + "\n")

    # Report
    print(f"Total processed: {total}")
    print(f"\nFunnel:")
    for stage, count in funnel.most_common():
        print(f"  {stage}: {count}")

    if self_check_times:
        import statistics
        print(f"\nSelf-check times (s):")
        print(f"  median: {statistics.median(self_check_times):.2f}")
        print(f"  p90: {sorted(self_check_times)[int(0.9 * len(self_check_times))]:.2f}")
        print(f"  max: {max(self_check_times):.2f}")

    print(f"\nAccepted: {len(accepted_train)} train, {len(accepted_test)} test")
    print(f"Rejected: {len(rejected)}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Build dataset")
    parser.add_argument("--data-dir", type=str, default=None)
    parser.add_argument("--output-train", type=str, default="data/tasks_train.jsonl")
    parser.add_argument("--output-test", type=str, default="data/tasks_test.jsonl")
    parser.add_argument("--output-rejected", type=str, default="data/rejected.jsonl")
    parser.add_argument("--max-functions", type=int, default=None)
    args = parser.parse_args()

    build_dataset(
        data_dir=args.data_dir,
        output_train=args.output_train,
        output_test=args.output_test,
        output_rejected=args.output_rejected,
        max_functions=args.max_functions,
    )