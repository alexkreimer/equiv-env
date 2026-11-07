#!/usr/bin/env python3
"""Quick demo: extract + harness + verify a single function from ExeBench.

Usage:
    python scripts/demo.py <function_name>            # self-equivalence check
    python scripts/demo.py <function_name> <c_file>    # verify against candidate
    python scripts/demo.py <function_name> --broken     # test with a deliberately wrong variant

Examples:
    python scripts/demo.py crashme
    python scripts/demo.py R90deg candidate.c
    python scripts/demo.py increment_16 --broken
"""

import sys
import os
import tempfile
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from equiv.extract import extract_from_exebench
from equiv.harness import generate_harness, generate_self_harness
from equiv.verify import verify, VerifyResult

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

def find_zst_files(data_dir: str) -> list[str]:
    """Find all .jsonl.zst files in data directory."""
    results = []
    for root, dirs, files in os.walk(data_dir):
        for f in files:
            if f.endswith(".jsonl.zst"):
                results.append(os.path.join(root, f))
    return results


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    func_name = sys.argv[1]
    candidate_src = None
    broken_mode = False

    for arg in sys.argv[2:]:
        if arg == "--broken":
            broken_mode = True
        elif os.path.isfile(arg):
            with open(arg) as f:
                candidate_src = f.read()

    # Find the function in ExeBench test data
    zst_files = find_zst_files(DATA_DIR)
    if not zst_files:
        print("No .jsonl.zst files found in data/. Download test data first:")
        print("  cd data && curl -L -O https://huggingface.co/datasets/jordiae/exebench/resolve/main/test_real.tar.gz")
        print("  tar xzf test_real.tar.gz")
        sys.exit(1)

    task = None
    for zst_path in zst_files:
        for result in extract_from_exebench(zst_path, "test"):
            if not result.eligible:
                continue
            if result.task.func_name == func_name:
                task = result.task
                break
        if task:
            break

    if task is None:
        print(f"Function '{func_name}' not found among eligible functions.")
        print("Try a different name. Available in test_real (sample):")
        found = set()
        for zst_path in zst_files:
            for result in extract_from_exebench(zst_path, "test"):
                if result.eligible:
                    found.add(result.task.func_name)
                    if len(found) >= 20:
                        break
            if len(found) >= 20:
                break
        for name in sorted(found)[:20]:
            print(f"  {name}")
        sys.exit(1)

    print(f"Found: {task.func_name}")
    print(f"  signature: {task.signature.return_type} {task.signature.params}")
    print(f"  pointer rules: {task.pointer_rules}")
    print(f"  loops: {task.n_loops}, loc: {task.loc}")
    print(f"  source: {task.task_id}")
    print()

    # Choose candidate
    if candidate_src:
        pass  # use provided candidate
    elif broken_mode:
        # Generate a simple broken variant
        if "=" in task.func_src:
            candidate_src = task.func_src.replace("=1;", "=2;").replace("= 1;", "= 2;")
        else:
            candidate_src = task.func_src.replace("return", "return 0; //")
        print(f"Broken candidate: {candidate_src.strip()[:120]}...")
    else:
        candidate_src = task.func_src
        print("Self-equivalence check (original vs identical copy)")

    print()

    # Generate harness
    harness = generate_harness(task, candidate_src)
    print(f"Harness generated: {len(harness.harness_src)} chars, unwind={harness.unwind}")

    # Write harness to file
    harness_path = None
    with tempfile.NamedTemporaryFile(
        suffix=".c", mode="w", prefix=f"equiv_{task.func_name}_", delete=False
    ) as f:
        f.write(harness.harness_src)
        harness_path = f.name

    print(f"Harness written to: {harness_path}")
    print()

    # Run CBMC
    print("Running CBMC...")
    flags = [
        "cbmc", "--arch", "x86_64",
        "--function", "harness",
        "--unwind", str(harness.unwind),
        "--unwinding-assertions",
        "--no-signed-overflow-check",
    ]

    # Only add --trace for non-equivalent (to get counterexample)
    result = subprocess.run(
        flags + [harness_path],
        capture_output=True, text=True, timeout=60,
    )

    # Parse results
    for line in result.stdout.split("\n"):
        if any(kw in line for kw in [
            "FAILURE", "SUCCESSFUL", "FAILED", "VERIFICATION",
            "assertion", "pointer_dereference", "overflow", "unwind",
        ]):
            print(f"  {line.strip()[:140]}")

    if result.returncode == 0:
        print("\nVERDICT: PROVEN EQUIVALENT")
    elif result.returncode == 10:
        print("\nVERDICT: NOT EQUIVALENT (or UB found)")
        # Re-run with trace for counterexample
        print("\nRe-running with --trace for counterexample...")
        result2 = subprocess.run(
            flags + ["--trace", harness_path],
            capture_output=True, text=True, timeout=60,
        )
        for line in result2.stdout.split("\n"):
            if "Trace for" in line or "State" in line or "Violated" in line:
                print(f"  {line.strip()[:140]}")
    else:
        print(f"\nCBMC error (exit {result.returncode})")

    if result.stderr:
        print(f"\nStderr: {result.stderr[:500]}")

    # Cleanup
    print(f"\nHarness file kept at: {harness_path}")
    print(f"Run manually: cbmc --function harness --unwind {harness.unwind} --unwinding-assertions {harness_path}")


if __name__ == "__main__":
    main()