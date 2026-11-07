"""Inspect ExeBench schema from downloaded zst files."""
import json
import subprocess

def read_first_n_zst(path, n=5):
    """Read first n JSON lines from a zst file."""
    result = subprocess.run(
        ["zstdcat", path], capture_output=True, text=True, timeout=30
    )
    lines = result.stdout.strip().split("\n")[:n]
    return [json.loads(line) for line in lines]

# Test synth
print("=== test_synth (first 3) ===")
examples = read_first_n_zst("data/synth_test/data_0_time1678160965_default.jsonl.zst", 3)
for i, ex in enumerate(examples):
    t = ex["text"]
    print(f"\n--- Example {i}: {t['fname']} ---")
    print(f"  path: {t['path']}")
    print(f"  func_head: {t['func_head']}")
    print(f"  signature: {t['signature']}")
    print(f"  func_def (first 200 chars): {t['func_def'][:200]!r}")
    # Check IO pairs
    for io_key in ["synth_io_pairs", "real_io_pairs"]:
        val = t.get(io_key)
        if val:
            print(f"  {io_key}: {len(val)} pairs")
            if val and isinstance(val[0], dict):
                print(f"    first pair keys: {list(val[0].keys())}")
                inp = val[0].get("input", [])
                if inp:
                    print(f"    first input: {inp[0] if inp else 'empty'}")
        else:
            print(f"  {io_key}: None")
    # Check deps
    for dep_key in ["synth_deps", "real_deps"]:
        val = t.get(dep_key)
        if val:
            print(f"  {dep_key}: {val[:100]!r}...")
        else:
            print(f"  {dep_key}: None")
    print(f"  real_error: {'present' if t.get('real_error') else 'none'}")
    print(f"  angha_error: {'present' if t.get('angha_error') else 'none'}")

# Check if any have synth_io_pairs
print("\n=== Checking IO pair availability (50 samples) ===")
examples = read_first_n_zst("data/synth_test/data_0_time1678160965_default.jsonl.zst", 50)
with_io = sum(1 for ex in examples if ex["text"].get("synth_io_pairs"))
print(f"test_synth: {with_io}/50 have synth_io_pairs")

# Test real
print("\n=== Checking test_real (50 samples) ===")
examples = read_first_n_zst("data/real_test/data_0_time1678114487_default.jsonl.zst", 50)
with_io = sum(1 for ex in examples if ex["text"].get("synth_io_pairs"))
print(f"test_real: {with_io}/50 have synth_io_pairs")
# Also show one that compiles
compiling = [ex for ex in examples if not ex["text"].get("real_error")]
print(f"test_real: {len(compiling)}/50 compile without error")
if compiling:
    ex = compiling[0]
    t = ex["text"]
    print(f"\n  Example compiling function: {t['fname']}")
    print(f"  func_head: {t['func_head']}")
    print(f"  signature: {t['signature']}")
    print(f"  func_def: {t['func_def'][:300]}")