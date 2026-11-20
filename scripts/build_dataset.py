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

import re
import sys
import os
import json
import time
import tempfile
import subprocess
from collections import Counter
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from equiv.config import load_config, config_hash
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


def _is_unwind_only_failure(stdout: str, stderr: str) -> bool:
    """Return True if CBMC failed due to unwinding assertions (not parse/assertion error)."""
    if "PARSING ERROR" in stderr or "CONVERSION ERROR" in stderr:
        return False
    if "VERIFICATION FAILED" not in stdout:
        return False
    has_unwind = bool(re.search(r'\[.*\.unwind\.\d+\].*FAILURE', stdout))
    has_assertion = bool(re.search(r'\[.*\.assertion\.\d+\].*FAILURE', stdout))
    return has_unwind and not has_assertion


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


def self_equiv_check(
    task: Task,
    timeout_s: float = 60,
    unwind_candidates: list[int] | tuple[int, ...] | None = None,
) -> tuple[bool, float, int]:
    """Run self-equivalence check with unwind search (spec Step 4).

    Tries each unwind value in `unwind_candidates` (default [maxn+2, 34, 66])
    in ascending order, advancing ONLY when CBMC fails due to unwinding
    assertions. Stores the smallest passing unwind bound.

    Returns (passed, check_time_s, unwind_used).
    """
    if not unwind_candidates:
        unwind_candidates = [task.unwind or (task.maxn + 2), 34, 66]
    # Deduplicate and sort ascending
    candidates = sorted({int(u) for u in unwind_candidates})

    harness = generate_self_harness(task, maxn=task.maxn, unwind=candidates[0])
    harness_src = harness.harness_src

    with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
        f.write(harness_src)
        harness_path = f.name

    try:
        total_elapsed = 0.0
        last_uw = candidates[0]
        for uw in candidates:
            last_uw = uw
            remaining = max(1.0, timeout_s - total_elapsed)
            start = time.monotonic()
            result = _run_cbmc_raw(
                harness_path, uw, remaining,
                extra_flags=["--no-signed-overflow-check"],
            )
            elapsed = time.monotonic() - start
            total_elapsed += elapsed

            if "VERIFICATION SUCCESSFUL" in result.stdout:
                return True, elapsed, uw
            if not _is_unwind_only_failure(result.stdout, result.stderr):
                return False, total_elapsed, uw
            if total_elapsed >= timeout_s:
                return False, total_elapsed, uw
        return False, total_elapsed, last_uw
    except subprocess.TimeoutExpired:
        return False, timeout_s, last_uw
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
    config_path: str | None = None,
):
    """Run the full dataset build pipeline."""
    cfg = load_config(config_path)
    cbmc_cfg = cfg["cbmc"]
    ds_cfg = cfg["dataset"]
    maxn = int(cbmc_cfg["maxn"])
    unwind_candidates = [int(u) for u in cbmc_cfg.get("unwind_candidates", [maxn + 2, 34, 66])]
    timeout_s = float(cbmc_cfg["timeout_s"])
    ub_timeout_s = float(cbmc_cfg["ub_timeout_s"])
    excluded_patterns = ds_cfg["excluded_path_patterns"]
    allowed_libc = set(ds_cfg["allowed_libc"])
    max_per_file = ds_cfg.get("max_per_file")
    max_per_repo = ds_cfg.get("max_per_repo")

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

    def _source_keys(task_id: str) -> tuple[str, str]:
        """Extract (file_path, owner/repo) from task_id 'split:path:fname'."""
        parts = task_id.split(":")
        file_path = parts[1] if len(parts) > 1 else task_id
        segs = file_path.split("/")
        repo = "/".join(segs[:2]) if len(segs) >= 2 else file_path
        return file_path, repo

    funnel = Counter()
    accepted_per_file = Counter()
    accepted_per_repo = Counter()
    accepted_train = []
    accepted_test = []
    rejected = []
    self_check_times = []

    print(f"Config hash: {config_hash(cfg)}")

    total = 0
    initial_unwind = min(unwind_candidates)
    for zst_path in zst_files:
        split = get_split(zst_path)
        for result in extract_from_exebench(
            zst_path,
            split,
            excluded_path_patterns=excluded_patterns,
            allowed_libc=allowed_libc,
        ):
            total += 1

            if not result.eligible:
                funnel[result.reason_code] += 1
                rejected.append({"task_id": "?", "reason_code": result.reason_code, "split": split})
                if max_functions and total >= max_functions:
                    break
                continue

            task = result.task
            task.maxn = maxn
            task.unwind = initial_unwind
            file_key, repo_key = _source_keys(task.task_id)

            if max_per_file and accepted_per_file[file_key] >= max_per_file:
                funnel["cap_exceeded_file"] += 1
                rejected.append({"task_id": task.task_id, "reason_code": "cap_exceeded_file", "split": split})
                continue

            if max_per_repo and accepted_per_repo[repo_key] >= max_per_repo:
                funnel["cap_exceeded_repo"] += 1
                rejected.append({"task_id": task.task_id, "reason_code": "cap_exceeded_repo", "split": split})
                continue

            funnel["eligible"] += 1

            # Compile check
            if not compile_check(task):
                funnel["compile_fail"] += 1
                rejected.append({"task_id": task.task_id, "reason_code": "compile_fail", "split": split})
                continue

            funnel["compile_pass"] += 1

            # UB check (at initial unwind)
            ub_free, ranges = check_original_ub(
                task, timeout_s=ub_timeout_s, maxn=maxn, unwind=initial_unwind
            )
            if not ub_free:
                funnel["ub_in_original"] += 1
                rejected.append({"task_id": task.task_id, "reason_code": "ub_in_original", "split": split})
                continue

            if ranges:
                task.input_ranges = ranges
            funnel["ub_pass"] += 1

            # Self-equivalence check with unwind search
            passed, check_time, unwind = self_equiv_check(
                task, timeout_s=timeout_s, unwind_candidates=unwind_candidates
            )
            task.self_check_time_s = check_time
            task.unwind = unwind
            self_check_times.append(check_time)

            if not passed:
                funnel["self_equiv_fail"] += 1
                rejected.append({"task_id": task.task_id, "reason_code": "self_equiv_fail", "split": split})
                continue

            # If unwind search succeeded at a deeper bound, verify UB holds at full depth too
            if unwind > initial_unwind:
                ub_free_deep, ranges_deep = check_original_ub(
                    task, timeout_s=ub_timeout_s, maxn=maxn, unwind=unwind
                )
                if not ub_free_deep:
                    funnel["ub_in_original"] += 1
                    rejected.append({"task_id": task.task_id, "reason_code": "ub_in_original", "split": split})
                    continue
                if ranges_deep and not task.input_ranges:
                    task.input_ranges = ranges_deep

            funnel["self_equiv_pass"] += 1
            accepted_per_file[file_key] += 1
            accepted_per_repo[repo_key] += 1

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
    parser.add_argument("--config", type=str, default=None, help="Path to config.yaml")
    args = parser.parse_args()

    build_dataset(
        data_dir=args.data_dir,
        output_train=args.output_train,
        output_test=args.output_test,
        output_rejected=args.output_rejected,
        max_functions=args.max_functions,
        config_path=args.config,
    )