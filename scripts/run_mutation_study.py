#!/usr/bin/env python3
"""Mutation study — validate the verifier by checking mutants."""

import sys, os, time, tempfile, subprocess
from collections import Counter
from dataclasses import dataclass
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from equiv.extract import extract_from_exebench, Task, Signature
from equiv.harness import generate_harness, _filter_deps, HEADER, _rename_function
from equiv.verify import verify
from equiv.mutate import mutate

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


@dataclass
class MutantResult:
    task_id: str; mutant_idx: int; operator: str; mutant_src: str
    test_verdict: str = "unknown"; cbmc_verdict: str = "unknown"
    ground_truth: str = "unknown"; counterexample_replay: str = "not_applicable"
    check_time_s: float = 0.0


def find_zst_files(data_dir: str) -> list[str]:
    results = []
    for root, dirs, files in os.walk(data_dir):
        for f in files:
            if f.endswith(".jsonl.zst"):
                results.append(os.path.join(root, f))
    return results


def _compile_and_run(driver_src: str, timeout_s: int = 30) -> str:
    """Compile and run a C test driver. Returns 'differs', 'same', or 'unknown'."""
    with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
        f.write(driver_src); src_path = f.name
    try:
        comp = subprocess.run(
            ["gcc", "-O0", "-fsigned-char", "-o", src_path + ".out", src_path],
            capture_output=True, text=True, timeout=10)
        if comp.returncode != 0: return "unknown"
        result = subprocess.run([src_path + ".out"], capture_output=True, text=True, timeout=timeout_s)
        if "DIFFERS" in result.stdout: return "differs"
        elif "SAME" in result.stdout: return "same"
        return "unknown"
    except subprocess.TimeoutExpired: return "unknown"
    finally:
        for p in [src_path, src_path + ".out"]:
            try: os.unlink(p)
            except OSError: pass


def heavy_random_test(task, original_src: str, mutant_src: str, n_inputs: int = 1000) -> str:
    """Heavy random testing to determine ground truth. Uses 1000 inputs (not 100k for speed)."""
    sig = task.signature

    orig = _rename_function(original_src, task.func_name, "orig_func") or original_src
    mut = _rename_function(mutant_src, task.func_name, "mut_func") or mutant_src

    # Build driver — try without deps first, then with deps
    for include_deps in [False, True]:
        parts = [HEADER, "#define MAXN 8"]
        if include_deps and task.deps_src:
            filtered = _filter_deps(task.deps_src)
            if filtered: parts.append(filtered)
        parts.append(orig); parts.append(mut)

        parts.append("#include <stdio.h>\n#include <stdlib.h>\nint main(void) { srand(42);")
        for _ in range(min(n_inputs, 1000)):
            parts.append("  {")
            for name, c_type in sig.params:
                if c_type.endswith("*"):
                    p = c_type.rstrip("*").replace("const ", "").strip()
                    parts.append(f"    {p} {name}_o[MAXN], {name}_m[MAXN];")
                    parts.append(f"    for (int __i = 0; __i < MAXN; __i++) {{ int __v = rand(); {name}_o[__i] = ({p})__v; {name}_m[__i] = ({p})__v; }}")
                else:
                    parts.append(f"    {c_type} {name} = ({c_type})rand();")
            oa = [f"{n}_o" if t.endswith("*") else n for n, t in sig.params]
            ma = [f"{n}_m" if t.endswith("*") else n for n, t in sig.params]
            if sig.return_type != "void":
                parts.append(f"    if (orig_func({', '.join(oa)}) != mut_func({', '.join(ma)})) {{ printf(\"DIFFERS\\n\"); return 1; }}")
            else:
                parts.append(f"    orig_func({', '.join(oa)}); mut_func({', '.join(ma)});")
            for name, c_type in sig.params:
                if c_type.endswith("*"):
                    parts.append(f"    for (int __i = 0; __i < MAXN; __i++) if ({name}_o[__i] != {name}_m[__i]) {{ printf(\"DIFFERS\\n\"); return 1; }}")
            parts.append("  }")
        parts.append('  printf("SAME\\n"); return 0; }')

        result = _compile_and_run("\n".join(parts))
        if result != "unknown": return result
    return "unknown"


def replay_counterexample(task, mutant_src: str, ce: dict) -> str:
    """Replay counterexample. Returns 'replayed' or 'failed'."""
    if not ce or not ce.get("inputs"): return "not_applicable"
    sig = task.signature; inputs = ce["inputs"]

    orig = _rename_function(task.func_src, task.func_name, "orig_func") or task.func_src
    mut = _rename_function(mutant_src, task.func_name, "mut_func") or mutant_src

    parts = [HEADER]
    if task.deps_src:
        filtered = _filter_deps(task.deps_src)
        if filtered: parts.append(filtered)
    parts.append(orig); parts.append(mut)
    parts.append("#include <stdio.h>\nint main(void) {")

    for name, c_type in sig.params:
        if c_type.endswith("*"):
            p = c_type.rstrip("*").replace("const ", "").strip()
            parts.append(f"  {p} {name}_o[8]={{0}}, {name}_m[8]={{0}};")
        else:
            parts.append(f"  {c_type} {name} = {inputs.get(name, '0')};")

    oa = [f"{n}_o" if t.endswith("*") else n for n, t in sig.params]
    ma = [f"{n}_m" if t.endswith("*") else n for n, t in sig.params]
    if sig.return_type != "void":
        parts.append(f"  if (orig_func({', '.join(oa)}) != mut_func({', '.join(ma)})) {{ printf(\"DIFFERS\\n\"); return 0; }}")
    else:
        parts.append(f"  orig_func({', '.join(oa)}); mut_func({', '.join(ma)});")
    for name, c_type in sig.params:
        if c_type.endswith("*"):
            parts.append(f"  for (int __i = 0; __i < 8; __i++) if ({name}_o[__i] != {name}_m[__i]) {{ printf(\"DIFFERS\\n\"); return 0; }}")
    parts.append('  printf("SAME\\n"); return 1; }')

    result = _compile_and_run("\n".join(parts), timeout_s=10)
    return "replayed" if result == "differs" else "failed"


def run_single_mutant(args) -> MutantResult:
    task, mutant_idx, mutant_src, operator = args
    r = MutantResult(task_id=task.task_id, mutant_idx=mutant_idx, operator=operator, mutant_src=mutant_src)
    start = time.monotonic()
    try:
        vr = verify(task, mutant_src, timeout_s=60, extra_cbmc_flags=None)
        r.cbmc_verdict = vr.status; r.check_time_s = time.monotonic() - start
        if vr.status == "not_equivalent" and vr.counterexample:
            r.counterexample_replay = replay_counterexample(task, mutant_src, vr.counterexample)
    except Exception as e:
        r.cbmc_verdict = f"error:{e}"; r.check_time_s = time.monotonic() - start; return r
    try:
        r.ground_truth = heavy_random_test(task, task.func_src, mutant_src)
    except Exception:
        r.ground_truth = "unknown"
    return r


def run_mutation_study(n_tasks=500, mutants_per_task=3, n_workers=1, data_dir=None, tasks_jsonl=None):
    if tasks_jsonl:
        import json as _json
        tasks = []
        with open(tasks_jsonl) as f:
            for line in f:
                d = _json.loads(line)
                sig_d = d["signature"]
                sig = Signature(name=sig_d["name"], return_type=sig_d["return_type"], params=[tuple(p) for p in sig_d["params"]])
                task = Task(
                    task_id=d["task_id"], source=d.get("source", "exebench"), split=d.get("split", "train"),
                    func_name=d["func_name"], func_src=d["func_src"], deps_src=d.get("deps_src", ""),
                    signature=sig, pointer_rules=d.get("pointer_rules", []),
                    maxn=d.get("maxn", 8), unwind=d.get("unwind", 10),
                    n_loops=d.get("n_loops", 0), loc=d.get("loc", 0),
                    io_tests=d.get("io_tests"), input_ranges=d.get("input_ranges"),
                    self_check_time_s=d.get("self_check_time_s", 0.0),
                )
                tasks.append(task)
                if len(tasks) >= n_tasks:
                    break
    else:
        if data_dir is None: data_dir = DATA_DIR
        zst_files = find_zst_files(data_dir)
        if not zst_files: print("No data files found."); return
        tasks = []
        for zst_path in zst_files:
            for result in extract_from_exebench(zst_path, "test"):
                if result.eligible: tasks.append(result.task)
                if len(tasks) >= n_tasks: break
            if len(tasks) >= n_tasks: break

    print(f"Running mutation study on {len(tasks)} tasks, {mutants_per_task} mutants each...")
    all_mutants = []
    for task in tasks:
        for mi, mutant in enumerate(mutate(task.func_src, n=mutants_per_task)):
            all_mutants.append((task, mi, mutant.src, mutant.operator))
    print(f"Total mutants: {len(all_mutants)}")

    results = []; start_time = time.monotonic()
    if n_workers <= 1:
        for i, args in enumerate(all_mutants):
            if i % 5 == 0: print(f"  {i}/{len(all_mutants)}")
            results.append(run_single_mutant(args))
    else:
        with ProcessPoolExecutor(max_workers=n_workers) as executor:
            futures = {executor.submit(run_single_mutant, a): i for i, a in enumerate(all_mutants)}
            for future in as_completed(futures): results.append(future.result())

    elapsed = time.monotonic() - start_time
    truly_different = [r for r in results if r.ground_truth == "differs"]
    truly_same = [r for r in results if r.ground_truth == "same"]
    false_proofs = [r for r in results if r.cbmc_verdict == "proven" and r.ground_truth == "differs"]
    ce_results = [r for r in results if r.counterexample_replay != "not_applicable"]

    kill_rate = (sum(1 for r in truly_different if r.cbmc_verdict == "not_equivalent") / len(truly_different) * 100) if truly_different else 0

    print(f"\n{'='*60}\nMUTATION STUDY RESULTS\n{'='*60}")
    print(f"Tasks: {len(tasks)}  Mutants: {len(results)}")
    print(f"Truly different: {len(truly_different)}  Truly same: {len(truly_same)}  Unknown: {len(results)-len(truly_different)-len(truly_same)}")
    print(f"Kill rate: {kill_rate:.1f}% (target: ≥95%)")
    print(f"False proofs: {len(false_proofs)} (target: 0)")
    if false_proofs:
        for fp in false_proofs[:5]: print(f"  FALSE PROOF: {fp.task_id} op={fp.operator}")
    print(f"CE replay: {sum(1 for r in ce_results if r.counterexample_replay=='replayed')}/{len(ce_results)} replayed")
    print(f"Time: {elapsed:.1f}s")
    return results


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--n-tasks", type=int, default=500)
    p.add_argument("--mutants-per-task", type=int, default=3)
    p.add_argument("--n-workers", type=int, default=1)
    p.add_argument("--data-dir", type=str, default=None)
    p.add_argument("--tasks-jsonl", type=str, default=None)
    args = p.parse_args()
    run_mutation_study(args.n_tasks, args.mutants_per_task, args.n_workers, args.data_dir, args.tasks_jsonl)