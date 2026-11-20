#!/usr/bin/env python3
"""Full dataset build pipeline (Stage 0, spec §5).

Pipeline per function:
1. extract + eligibility → reject with reason, or continue
2. compile check (gcc/clang, -O0/-O3)
3. original-only UB check (CBMC safety checks)
4. self-equivalence check with unwind search
5. accept → write Task to tasks_{split}.jsonl

Usage:
    python scripts/build_dataset.py --data-dir data/ --workers 6 --target-train 5500
"""

from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from equiv.config import config_hash, load_config
from equiv.extract import (
    Task,
    _normalize_whitespace,
    extract_from_exebench,
    has_input_dependent_loop,
)
from equiv.harness import _filter_deps, _header_for_task, generate_self_harness
from equiv.verify import _run_cbmc_raw, check_original_ub

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


def find_zst_files(data_dir: str, include_synth: bool = False) -> list[str]:
    """Find .jsonl.zst dataset archives, skipping synthetic splits by default."""
    results = []
    for root, dirs, files in os.walk(data_dir):
        dirs.sort()
        if not include_synth:
            dirs[:] = [d for d in dirs if "synth" not in d.lower()]
        for f in sorted(files):
            if not f.endswith(".jsonl.zst"):
                continue
            full = os.path.join(root, f)
            if not include_synth and "synth" in full.lower():
                continue
            results.append(full)
    return results


def _func_dedup_hash(func_src: str) -> str:
    """Compute a normalized sha256 hash of a C function for deduplication."""
    norm = _normalize_whitespace(func_src)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


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
    filtered_deps = _filter_deps(task.deps_src or "")
    full_src = _header_for_task(task) + "\n" + filtered_deps + "\n" + src

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


def _verify_candidate_task(
    task: Task,
    fhash: str | None,
    file_key: str,
    repo_key: str,
    ub_timeout_s: float,
    timeout_s: float,
    maxn: int,
    unwind_candidates: list[int],
) -> tuple[str, Task, str | None, str, str, float]:
    """Worker entrypoint: compile_check -> check_original_ub -> self_equiv_check."""
    initial_unwind = min(unwind_candidates)
    if not compile_check(task):
        return ("compile_fail", task, fhash, file_key, repo_key, 0.0)

    ub_free, ranges = check_original_ub(
        task, timeout_s=ub_timeout_s, maxn=maxn, unwind=initial_unwind
    )
    if not ub_free:
        return ("ub_in_original", task, fhash, file_key, repo_key, 0.0)
    if ranges:
        task.input_ranges = ranges

    passed, check_time, unwind = self_equiv_check(
        task, timeout_s=timeout_s, unwind_candidates=unwind_candidates
    )
    task.self_check_time_s = check_time
    task.unwind = unwind
    if not passed:
        return ("self_equiv_fail", task, fhash, file_key, repo_key, check_time)

    if unwind > initial_unwind:
        ub_free_deep, ranges_deep = check_original_ub(
            task, timeout_s=ub_timeout_s, maxn=maxn, unwind=unwind
        )
        if not ub_free_deep:
            return ("ub_in_original_deep", task, fhash, file_key, repo_key, check_time)
        if ranges_deep and not task.input_ranges:
            task.input_ranges = ranges_deep

    return ("accepted", task, fhash, file_key, repo_key, check_time)


def task_to_dict(t: Task) -> dict:
    d = asdict(t)
    d["signature"] = {
        "name": t.signature.name,
        "return_type": t.signature.return_type,
        "params": t.signature.params,
    }
    d["has_input_dependent_loop"] = has_input_dependent_loop(t.func_src, t.signature)
    return d


def build_dataset(
    data_dir: str | None = None,
    output_train: str = "data/tasks_train.jsonl",
    output_test: str = "data/tasks_test.jsonl",
    output_rejected: str = "data/rejected.jsonl",
    max_functions: int | None = None,
    max_train: int | None = None,
    max_test: int | None = None,
    target_train: int | None = None,
    target_test: int | None = None,
    workers: int = 1,
    include_synth: bool = False,
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
    deduplicate = bool(ds_cfg.get("deduplicate", True))

    if data_dir is None:
        data_dir = DATA_DIR

    zst_files = find_zst_files(data_dir, include_synth=include_synth)
    if not zst_files:
        print("No .jsonl.zst files found.")
        return

    def get_split(path: str) -> str:
        low = path.lower()
        if "test_real" in low or "real_test" in low or "test_synth" in low or "synth_test" in low:
            return "test"
        if "valid_real" in low or "real_valid" in low:
            return "test"  # add valid to test pool per Task C
        basename = os.path.basename(low)
        if "test" in basename or "valid" in basename:
            return "test"
        return "train"

    def _source_keys(task_id: str) -> tuple[str, str]:
        parts = task_id.split(":")
        file_path = parts[1] if len(parts) > 1 else task_id
        segs = file_path.split("/")
        repo = "/".join(segs[:2]) if len(segs) >= 2 else file_path
        return file_path, repo

    # Process train files first so deduplication drops any test leak against train
    train_files = [p for p in zst_files if get_split(p) == "train"]
    test_files = [p for p in zst_files if get_split(p) == "test"]

    funnel = Counter()
    accepted_per_file = Counter()
    accepted_per_repo = Counter()
    seen_train_hashes: set[str] = set()
    seen_test_hashes: set[str] = set()
    accepted_train: list[Task] = []
    accepted_test: list[Task] = []
    rejected: list[dict] = []
    self_check_times: list[float] = []

    os.makedirs(os.path.dirname(output_train) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(output_test) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(output_rejected) or ".", exist_ok=True)

    f_train = open(output_train, "w")
    f_test = open(output_test, "w")
    f_rej = open(output_rejected, "w")

    print(f"Config hash: {config_hash(cfg)} | workers={workers} | files={len(train_files)} train, {len(test_files)} test", flush=True)

    total = 0
    seen_per_split = Counter()
    initial_unwind = min(unwind_candidates)
    start_wall = time.monotonic()

    def _record_reject(rec: dict):
        rejected.append(rec)
        f_rej.write(json.dumps(rec) + "\n")

    def _handle_outcome(outcome, split: str):
        status, task, fhash, file_key, repo_key, check_time = outcome
        if status == "compile_fail":
            funnel["compile_fail"] += 1
            _record_reject({"task_id": task.task_id, "reason_code": "compile_fail", "split": split})
            return
        funnel["compile_pass"] += 1

        if status == "ub_in_original":
            funnel["ub_in_original"] += 1
            _record_reject({"task_id": task.task_id, "reason_code": "ub_in_original", "split": split})
            return
        funnel["ub_pass"] += 1

        self_check_times.append(check_time)
        if status == "self_equiv_fail":
            funnel["self_equiv_fail"] += 1
            _record_reject({"task_id": task.task_id, "reason_code": "self_equiv_fail", "split": split})
            return
        if status == "ub_in_original_deep":
            funnel["ub_in_original"] += 1
            _record_reject({"task_id": task.task_id, "reason_code": "ub_in_original", "split": split})
            return

        # Re-check caps and target limits when concurrent workers finish
        target_cap = target_train if split == "train" else target_test
        acc_list = accepted_train if split == "train" else accepted_test
        if target_cap is not None and len(acc_list) >= target_cap:
            return
        if max_per_file and accepted_per_file[file_key] >= max_per_file:
            funnel["cap_exceeded_file"] += 1
            _record_reject({"task_id": task.task_id, "reason_code": "cap_exceeded_file", "split": split})
            return
        if max_per_repo and accepted_per_repo[repo_key] >= max_per_repo:
            funnel["cap_exceeded_repo"] += 1
            _record_reject({"task_id": task.task_id, "reason_code": "cap_exceeded_repo", "split": split})
            return
        if deduplicate and fhash is not None:
            if split == "train":
                if fhash in seen_train_hashes:
                    funnel["duplicate"] += 1
                    _record_reject({"task_id": task.task_id, "reason_code": "duplicate", "split": split})
                    return
                seen_train_hashes.add(fhash)
            else:
                if fhash in seen_train_hashes or fhash in seen_test_hashes:
                    funnel["duplicate"] += 1
                    _record_reject({"task_id": task.task_id, "reason_code": "duplicate", "split": split})
                    return
                seen_test_hashes.add(fhash)

        funnel["self_equiv_pass"] += 1
        accepted_per_file[file_key] += 1
        accepted_per_repo[repo_key] += 1
        acc_list.append(task)
        out_fh = f_train if split == "train" else f_test
        out_fh.write(json.dumps(task_to_dict(task)) + "\n")
        out_fh.flush()

        if len(acc_list) % 100 == 0:
            n_loops = sum(1 for t in acc_list if t.n_loops > 0)
            elapsed = time.monotonic() - start_wall
            print(
                f"  [{split}] accepted={len(acc_list)} (loops={n_loops}) "
                f"seen={seen_per_split[split]} elapsed={elapsed:.0f}s",
                flush=True,
            )

    def _run_split(split: str, files: list[str]):
        nonlocal total
        split_limit = max_train if split == "train" else max_test
        target_cap = target_train if split == "train" else target_test
        acc_list = accepted_train if split == "train" else accepted_test

        def _hit_limit() -> bool:
            if target_cap is not None and len(acc_list) >= target_cap:
                return True
            if split_limit is not None and seen_per_split[split] >= split_limit:
                return True
            if max_functions and max_train is None and max_test is None and total >= max_functions:
                return True
            return False

        pool = ProcessPoolExecutor(max_workers=workers) if workers > 1 else None
        pending = set()
        max_inflight = max(1, workers * 3)

        try:
            for zst_path in files:
                if _hit_limit():
                    break
                for result in extract_from_exebench(
                    zst_path,
                    split,
                    excluded_path_patterns=excluded_patterns,
                    allowed_libc=allowed_libc,
                ):
                    while pool and len(pending) >= max_inflight:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED)
                        for fut in done:
                            _handle_outcome(fut.result(), split)

                    if _hit_limit():
                        break

                    total += 1
                    seen_per_split[split] += 1

                    if not result.eligible:
                        funnel[result.reason_code] += 1
                        _record_reject({"task_id": "?", "reason_code": result.reason_code, "split": split})
                        if _hit_limit():
                            break
                        continue

                    task = result.task
                    task.maxn = maxn
                    task.unwind = initial_unwind
                    file_key, repo_key = _source_keys(task.task_id)

                    if deduplicate:
                        fhash = _func_dedup_hash(task.func_src)
                        if split == "train" and fhash in seen_train_hashes:
                            funnel["duplicate"] += 1
                            _record_reject({"task_id": task.task_id, "reason_code": "duplicate", "split": split})
                            if _hit_limit():
                                break
                            continue
                        if split != "train" and (fhash in seen_train_hashes or fhash in seen_test_hashes):
                            funnel["duplicate"] += 1
                            _record_reject({"task_id": task.task_id, "reason_code": "duplicate", "split": split})
                            if _hit_limit():
                                break
                            continue
                    else:
                        fhash = None

                    if max_per_file and accepted_per_file[file_key] >= max_per_file:
                        funnel["cap_exceeded_file"] += 1
                        _record_reject({"task_id": task.task_id, "reason_code": "cap_exceeded_file", "split": split})
                        if _hit_limit():
                            break
                        continue

                    if max_per_repo and accepted_per_repo[repo_key] >= max_per_repo:
                        funnel["cap_exceeded_repo"] += 1
                        _record_reject({"task_id": task.task_id, "reason_code": "cap_exceeded_repo", "split": split})
                        if _hit_limit():
                            break
                        continue

                    funnel["eligible"] += 1

                    if pool is None:
                        outcome = _verify_candidate_task(
                            task, fhash, file_key, repo_key,
                            ub_timeout_s, timeout_s, maxn, unwind_candidates,
                        )
                        _handle_outcome(outcome, split)
                    else:
                        fut = pool.submit(
                            _verify_candidate_task,
                            task, fhash, file_key, repo_key,
                            ub_timeout_s, timeout_s, maxn, unwind_candidates,
                        )
                        pending.add(fut)

                    if _hit_limit():
                        break

            # Drain remaining in-flight tasks for this split
            if pool and pending:
                for fut in list(pending):
                    _handle_outcome(fut.result(), split)
                pending.clear()
        finally:
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)

    try:
        if train_files and (max_train is None or max_train > 0) and (target_train is None or target_train > 0):
            _run_split("train", train_files)
        if test_files and (max_test is None or max_test > 0) and (target_test is None or target_test > 0):
            if not (max_functions and max_train is None and max_test is None and total >= max_functions):
                _run_split("test", test_files)
    finally:
        f_train.close()
        f_test.close()
        f_rej.close()

    # Report
    print(f"Total processed: {total} (train={seen_per_split['train']}, test={seen_per_split['test']})")
    print(f"\nFunnel:")
    for stage, count in funnel.most_common():
        print(f"  {stage}: {count}")

    if self_check_times:
        import statistics
        print(f"\nSelf-check times (s):")
        print(f"  median: {statistics.median(self_check_times):.2f}")
        print(f"  p90: {sorted(self_check_times)[int(0.9 * len(self_check_times))]:.2f}")
        print(f"  max: {max(self_check_times):.2f}")

    all_accepted = accepted_train + accepted_test
    n_with_loops = sum(1 for t in all_accepted if t.n_loops > 0)
    n_input_dep_loops = sum(1 for t in all_accepted if has_input_dependent_loop(t.func_src, t.signature))
    n_paired = sum(1 for t in all_accepted if "paired" in (t.pointer_rules or {}).values())
    n_unwind_gt10 = sum(1 for t in all_accepted if t.unwind > initial_unwind)
    print(f"\nAccepted: {len(accepted_train)} train, {len(accepted_test)} test (total {len(all_accepted)})")
    if all_accepted:
        print(f"  with loops: {n_with_loops} ({100*n_with_loops/len(all_accepted):.1f}%)")
        print(f"  input-dependent loops: {n_input_dep_loops} ({100*n_input_dep_loops/len(all_accepted):.1f}%)")
        print(f"  paired (ptr, len): {n_paired} ({100*n_paired/len(all_accepted):.1f}%)")
        print(f"  unwind > {initial_unwind}: {n_unwind_gt10}")
    print(f"Rejected: {len(rejected)}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Build dataset")
    parser.add_argument("--data-dir", type=str, default=None)
    parser.add_argument("--output-train", type=str, default="data/tasks_train.jsonl")
    parser.add_argument("--output-test", type=str, default="data/tasks_test.jsonl")
    parser.add_argument("--output-rejected", type=str, default="data/rejected.jsonl")
    parser.add_argument("--max-functions", type=int, default=None)
    parser.add_argument("--max-train", type=int, default=None)
    parser.add_argument("--max-test", type=int, default=None)
    parser.add_argument("--target-train", type=int, default=None, help="Stop train once N tasks accepted")
    parser.add_argument("--target-test", type=int, default=None, help="Stop test once N tasks accepted")
    parser.add_argument("--workers", type=int, default=1, help="Number of parallel CBMC worker processes")
    parser.add_argument("--include-synth", action="store_true", help="Include synthetic splits (default: real only)")
    parser.add_argument("--config", type=str, default=None, help="Path to config.yaml")
    args = parser.parse_args()

    build_dataset(
        data_dir=args.data_dir,
        output_train=args.output_train,
        output_test=args.output_test,
        output_rejected=args.output_rejected,
        max_functions=args.max_functions,
        max_train=args.max_train,
        max_test=args.max_test,
        target_train=args.target_train,
        target_test=args.target_test,
        workers=args.workers,
        include_synth=args.include_synth,
        config_path=args.config,
    )
