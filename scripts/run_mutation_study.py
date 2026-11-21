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
from equiv.replay import replay_counterexample

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
    """Heavy random testing to determine ground truth within the harness's input domain."""
    from equiv.extract import get_paired_length_params
    from equiv.harness import _header_for_task, _pointee_type, _clean_type_str, _type_bounds
    from equiv.replay import _strip_inline

    sig = task.signature
    pointer_rules = task.pointer_rules or {}
    paired_lengths = get_paired_length_params(sig, pointer_rules)
    input_ranges = task.input_ranges or {}
    narrowed = bool(input_ranges.get("_narrowed"))
    buf_range = input_ranges.get("_buf_range", (-32768, 32767))

    def _rand_in_range(c_type: str, lo: int, hi: int) -> str:
        tmin, tmax, _ = _type_bounds(c_type)
        clo, chi = max(lo, tmin), min(hi, tmax)
        span = max(1, chi - clo + 1)
        return f"({_clean_type_str(c_type)})({clo}LL + (long long)((unsigned long long)rand() % {span}ULL))"

    orig = _strip_inline(_rename_function(original_src, task.func_name, "orig_func") or original_src)
    mut = _strip_inline(_rename_function(mutant_src, task.func_name, "mut_func") or mutant_src)

    # Build driver — try without deps first, then with deps
    for include_deps in [False, True]:
        parts = [_header_for_task(task), "#define MAXN 8"]
        if include_deps and task.deps_src:
            filtered = _filter_deps(task.deps_src)
            if filtered: parts.append(filtered)
        parts.append(orig); parts.append(mut)

        parts.append("#include <stdio.h>\n#include <stdlib.h>\nint main(void) { srand(42);")
        for _ in range(min(n_inputs, 1000)):
            parts.append("  {")
            for name, c_type in sig.params:
                if c_type.strip().endswith("*"):
                    p = _pointee_type(c_type)
                    parts.append(f"    {p} {name}_o[MAXN], {name}_m[MAXN];")
                    if narrowed and pointer_rules.get(name) != "string":
                        val_expr = _rand_in_range(p, int(buf_range[0]), int(buf_range[1]))
                    else:
                        val_expr = f"({p})rand()"
                    parts.append(f"    for (int __i = 0; __i < MAXN; __i++) {{ {p} __v = {val_expr}; {name}_o[__i] = __v; {name}_m[__i] = __v; }}")
                    if pointer_rules.get(name) == "string":
                        parts.append(f"    int __z_{name} = rand() % MAXN; {name}_o[__z_{name}] = 0; {name}_m[__z_{name}] = 0;")
                elif name in paired_lengths:
                    parts.append(f"    {_clean_type_str(c_type)} {name} = ({_clean_type_str(c_type)})(rand() % (MAXN + 1));")
                elif name in input_ranges and isinstance(input_ranges[name], (list, tuple)) and len(input_ranges[name]) == 2:
                    expr = _rand_in_range(c_type, int(input_ranges[name][0]), int(input_ranges[name][1]))
                    parts.append(f"    {_clean_type_str(c_type)} {name} = {expr};")
                elif narrowed:
                    expr = _rand_in_range(c_type, -32768, 32767)
                    parts.append(f"    {_clean_type_str(c_type)} {name} = {expr};")
                else:
                    decl = _clean_type_str(c_type)
                    _, _, is_unsigned = _type_bounds(decl)
                    if not is_unsigned and decl in ("int", "signed", "signed int", "long", "long long", "int32_t", "int64_t", "ssize_t"):
                        expr = _rand_in_range(c_type, -1073741823, 1073741823)
                    else:
                        expr = f"({decl})rand()"
                    parts.append(f"    {decl} {name} = {expr};")
            oa = [f"{n}_o" if t.strip().endswith("*") else n for n, t in sig.params]
            ma = [f"{n}_m" if t.strip().endswith("*") else n for n, t in sig.params]
            ret = _clean_type_str(sig.return_type)
            if ret != "void" and not ret.endswith("*"):
                parts.append(f"    if (orig_func({', '.join(oa)}) != mut_func({', '.join(ma)})) {{ printf(\"DIFFERS\\n\"); return 1; }}")
            else:
                parts.append(f"    orig_func({', '.join(oa)}); mut_func({', '.join(ma)});")
            for name, c_type in sig.params:
                if c_type.strip().endswith("*"):
                    parts.append(f"    for (int __i = 0; __i < MAXN; __i++) if ({name}_o[__i] != {name}_m[__i]) {{ printf(\"DIFFERS\\n\"); return 1; }}")
            parts.append("  }")
        parts.append('  printf("SAME\\n"); return 0; }')

        result = _compile_and_run("\n".join(parts))
        if result != "unknown": return result
    return "unknown"


def run_single_mutant(args) -> MutantResult:
    task, mutant_idx, mutant_src, operator = args
    r = MutantResult(task_id=task.task_id, mutant_idx=mutant_idx, operator=operator, mutant_src=mutant_src)
    start = time.monotonic()
    try:
        vr = verify(task, mutant_src, timeout_s=60, extra_cbmc_flags=None)
        r.cbmc_verdict = vr.status; r.check_time_s = time.monotonic() - start
        if vr.status == "not_equivalent" and vr.counterexample:
            r.counterexample_replay = replay_counterexample(task, mutant_src, vr.counterexample).status
    except Exception as e:
        r.cbmc_verdict = f"error:{e}"; r.check_time_s = time.monotonic() - start; return r
    try:
        r.ground_truth = heavy_random_test(task, task.func_src, mutant_src)
    except Exception:
        r.ground_truth = "unknown"
    try:
        from equiv.testgen import run_tests
        tr = run_tests(task, mutant_src, maxn=task.maxn or 8, seed=r.mutant_idx)
        r.test_verdict = "pass" if tr.passed else "fail"
    except Exception:
        r.test_verdict = "error"
    return r


def escape_rates(results) -> dict:
    """Which checks catch what CBMC refutes (Stage 0.5 P1).

    refuted   = CBMC says not_equivalent; confirmed = and the counterexample replays.
    cheap_escape: refuted, but the cheap tests (the evaluate() pre-filter) pass.
    heavy_escape: refuted, but heavy in-domain random testing finds no difference.
    """
    def _block(rs):
        n = len(rs)
        cheap = sum(1 for r in rs if r.test_verdict == "pass")
        heavy = sum(1 for r in rs if r.ground_truth == "same")
        both = sum(1 for r in rs if r.test_verdict == "pass" and r.ground_truth == "same")
        pct = lambda k: round(100.0 * k / n, 2) if n else 0.0
        return {"n": n, "cheap_escape": cheap, "cheap_escape_pct": pct(cheap),
                "heavy_escape": heavy, "heavy_escape_pct": pct(heavy),
                "both_escape": both, "both_escape_pct": pct(both)}

    refuted = [r for r in results if r.cbmc_verdict == "not_equivalent"]
    confirmed = [r for r in refuted if r.counterexample_replay == "replayed"]
    passed_cheap = [r for r in results if r.test_verdict == "pass"]
    refuted_given_cheap = sum(1 for r in passed_cheap if r.cbmc_verdict == "not_equivalent")
    return {
        "refuted": _block(refuted),
        "confirmed_refuted": _block(confirmed),
        "test_verdicts": dict(Counter(r.test_verdict for r in results)),
        # P(CBMC refutes | passes cheap tests): the mutation analogue of H_cheap
        "h_cheap_pct": round(100.0 * refuted_given_cheap / len(passed_cheap), 2) if passed_cheap else 0.0,
        "n_passed_cheap": len(passed_cheap),
    }


def run_mutation_study(
    n_tasks=500,
    mutants_per_task=3,
    n_workers=1,
    data_dir=None,
    tasks_jsonl=None,
    loops_only=False,
    seed=None,
    json_out=None,
):
    if seed is not None:
        import random
        random.seed(seed)

    if tasks_jsonl:
        import json as _json
        tasks = []
        with open(tasks_jsonl) as f:
            for line in f:
                if not line.strip():
                    continue
                d = _json.loads(line)
                if loops_only and d.get("n_loops", 0) <= 0:
                    continue
                sig_d = d["signature"]
                sig = Signature(name=sig_d["name"], return_type=sig_d["return_type"], params=[tuple(p) for p in sig_d["params"]])
                task = Task(
                    task_id=d["task_id"], source=d.get("source", "exebench"), split=d.get("split", "train"),
                    func_name=d["func_name"], func_src=d["func_src"], deps_src=d.get("deps_src", ""),
                    signature=sig, pointer_rules=d.get("pointer_rules") or {},
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
                if result.eligible:
                    if loops_only and result.task.n_loops <= 0:
                        continue
                    tasks.append(result.task)
                if len(tasks) >= n_tasks: break
            if len(tasks) >= n_tasks: break

    mode_str = " (loops-only)" if loops_only else ""
    print(f"Running mutation study on {len(tasks)} tasks{mode_str}, {mutants_per_task} mutants each...")
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
    by_status = Counter(r.counterexample_replay for r in ce_results)

    print(f"\n{'='*60}\nMUTATION STUDY RESULTS{mode_str.upper()}\n{'='*60}")
    print(f"Tasks: {len(tasks)}  Mutants: {len(results)}")
    print(f"Truly different: {len(truly_different)}  Truly same: {len(truly_same)}  Unknown: {len(results)-len(truly_different)-len(truly_same)}")
    print(f"Kill rate: {kill_rate:.1f}% (target: ≥95%)")
    print(f"False proofs: {len(false_proofs)} (target: 0)")
    if false_proofs:
        for fp in false_proofs[:5]: print(f"  FALSE PROOF: {fp.task_id} op={fp.operator}")
    print(f"CE replay: {by_status['replayed']}/{len(ce_results)} replayed "
          f"({by_status['not_reproduced']} not_reproduced = possible checker bugs, "
          f"{by_status['driver_error']} driver_error = replay tooling problems)")
    esc = escape_rates(results)
    for label in ("refuted", "confirmed_refuted"):
        b = esc[label]
        print(f"Escapes among {label} ({b['n']}): cheap tests {b['cheap_escape']} ({b['cheap_escape_pct']}%), "
              f"heavy random {b['heavy_escape']} ({b['heavy_escape_pct']}%), both {b['both_escape']} ({b['both_escape_pct']}%)")
    print(f"P(CBMC refutes | passes cheap tests): {esc['h_cheap_pct']}% of {esc['n_passed_cheap']}")
    print(f"Time: {elapsed:.1f}s")

    if json_out:
        import json as _json
        summary = {
            "tasks": len(tasks),
            "loops_only": loops_only,
            "mutants": len(results),
            "truly_different": len(truly_different),
            "truly_same": len(truly_same),
            "unknown": len(results) - len(truly_different) - len(truly_same),
            "kill_rate_pct": round(kill_rate, 2),
            "false_proofs": len(false_proofs),
            "ce_total": len(ce_results),
            "ce_replayed": by_status["replayed"],
            "ce_not_reproduced": by_status["not_reproduced"],
            "ce_driver_error": by_status["driver_error"],
            "elapsed_s": round(elapsed, 2),
            "escapes": esc,
        }
        with open(json_out, "w") as out:
            _json.dump(summary, out, indent=2)
    return results


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--n-tasks", type=int, default=500)
    p.add_argument("--mutants-per-task", type=int, default=3)
    p.add_argument("--n-workers", type=int, default=1)
    p.add_argument("--data-dir", type=str, default=None)
    p.add_argument("--tasks-jsonl", type=str, default=None)
    p.add_argument("--loops-only", action="store_true", help="Filter to tasks with n_loops > 0")
    p.add_argument("--seed", type=int, default=None, help="Random seed for reproducible mutant sampling")
    p.add_argument("--json-out", type=str, default=None, help="Write summary JSON to path")
    args = p.parse_args()
    run_mutation_study(
        args.n_tasks, args.mutants_per_task, args.n_workers,
        args.data_dir, args.tasks_jsonl,
        loops_only=args.loops_only, seed=args.seed, json_out=args.json_out,
    )