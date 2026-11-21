#!/usr/bin/env python3
"""CI self-equivalence check: runs CBMC on accepted tasks from a JSONL fixture."""
import sys, os, json, tempfile, subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from equiv.extract import Task, Signature
from equiv.harness import generate_self_harness

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "tasks_200.jsonl")


def load_tasks(path):
    tasks = []
    with open(path) as f:
        for line in f:
            d = json.loads(line)
            sig_d = d["signature"]
            sig = Signature(
                name=sig_d["name"],
                return_type=sig_d["return_type"],
                params=[tuple(p) for p in sig_d["params"]],
            )
            task = Task(
                task_id=d["task_id"],
                source=d.get("source", "exebench"),
                split=d.get("split", "train"),
                func_name=d["func_name"],
                func_src=d["func_src"],
                deps_src=d.get("deps_src", ""),
                signature=sig,
                pointer_rules=d.get("pointer_rules", {}),
                maxn=d.get("maxn", 8),
                unwind=d.get("unwind", 10),
                input_ranges=d.get("input_ranges"),
                n_loops=d.get("n_loops", 0),
                loc=d.get("loc", 0),
            )
            tasks.append(task)
    return tasks


def main():
    tasks = load_tasks(FIXTURE)
    print(f"Loaded {len(tasks)} tasks from fixture")

    passed = 0
    failed = 0
    errors = []
    times = []

    for i, task in enumerate(tasks):
        harness = generate_self_harness(task, maxn=task.maxn, unwind=task.unwind)
        with tempfile.NamedTemporaryFile(
            suffix=".c", mode="w", delete=False
        ) as f:
            f.write(harness.harness_src)
            tmp = f.name

        try:
            import time

            start = time.monotonic()
            result = subprocess.run(
                [
                    "cbmc", "--arch", "x86_64",
                    "--function", "harness",
                    "--unwind", str(harness.unwind),
                    "--unwinding-assertions",
                    "--no-signed-overflow-check",
                    tmp,
                ],
                capture_output=True, text=True, timeout=60,
            )
            elapsed = time.monotonic() - start
            times.append(elapsed)

            if "VERIFICATION SUCCESSFUL" in result.stdout:
                passed += 1
            else:
                failed += 1
                errors.append((task.task_id, result.stdout[-200:], result.stderr[-200:]))
        except subprocess.TimeoutExpired:
            failed += 1
            errors.append((task.task_id, "TIMEOUT", ""))
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(tasks)} — {passed} passed, {failed} failed")

    import statistics

    print(f"\nResults: {passed}/{len(tasks)} passed ({passed/len(tasks)*100:.1f}%)")
    if times:
        print(f"Times: median={statistics.median(times):.2f}s  p90={sorted(times)[int(0.9*len(times))]:.2f}s")

    if failed > 0:
        print(f"\nFailures ({len(errors)}):")
        for tid, out, err in errors[:5]:
            print(f"  {tid}: {out[:150]}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())