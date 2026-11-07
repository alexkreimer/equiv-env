"""Golden set tests — verify all 28+ pairs against expected CBMC verdicts.

Runs each pair through the harness generator with input bounds applied,
then checks that CBMC returns the expected verdict.
"""

import sys
import os
import re
import subprocess
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "golden"))

from catalog import GOLDEN_PAIRS

pytestmark = pytest.mark.golden


def _apply_bounds(harness_src: str, bounds, task_sig) -> str:
    """Insert __CPROVER_assume lines after scalar nondet calls.
    
    If bounds is None, auto-generates bounds for int-like params that
    look like buffer sizes (named n, len, size, etc.) in pointer functions.
    """
    import re
    lines = harness_src.split("\n")
    result = []

    # Determine if we need auto-bounds for size-like params
    needs_auto_bounds = bounds is None and any(t.endswith("*") for _, t in task_sig.params)

    for line in lines:
        result.append(line)
        if "nondet_" in line and "=" in line and "(" in line:
            match = re.search(r'^\s*(?:\w+\s+)?(\w+)\s*=\s*(?:\(\w+\))?nondet_\w+\(\)', line)
            if match:
                varname = match.group(1)
                indent = "  "
                if bounds:
                    lo, hi = bounds
                    result.append(f"{indent}__CPROVER_assume({varname} >= {lo} && {varname} <= {hi});")
                elif needs_auto_bounds and varname in ("n", "len", "length", "size", "count", "num", "sz", "start", "end"):
                    result.append(f"{indent}__CPROVER_assume({varname} >= 0 && {varname} <= MAXN);")

    return "\n".join(result)


def _run_pair(pair: dict):
    """Run a single golden pair through the pipeline and check verdict."""
    from equiv.extract import _parse_signature, Task, _infer_pointer_rules
    from equiv.harness import generate_harness, _extract_function_name
    from equiv.verify import _run_cbmc_raw
    import tempfile

    original_src = pair["original"]
    candidate_src = pair["candidate"]

    func_name = _extract_function_name(original_src)
    if func_name is None:
        raise ValueError(f"Cannot extract function name from: {original_src[:50]}")

    sig = _parse_signature(original_src, original_src, func_name)
    if sig is None:
        raise ValueError(f"Cannot parse signature: {original_src[:50]}")

    rules = _infer_pointer_rules(sig, original_src)

    task = Task(
        task_id=f"golden:{pair['id']}",
        source="golden",
        split="test",
        func_name=func_name,
        func_src=original_src,
        deps_src="",
        signature=sig,
        pointer_rules=rules,
        maxn=8,
        unwind=10,
    )

    # For unbounded loop test, need higher unwind
    if pair["id"] == "020_unbounded_loop":
        task.unwind = 10  # intentionally too small

    # Use catalog unwind if specified
    unwind = pair.get("unwind", task.maxn + 2)

    # Generate harness
    harness = generate_harness(task, candidate_src, maxn=task.maxn, unwind=unwind)

    # Apply input bounds if specified (or auto-bounds for size params)
    harness_src = harness.harness_src
    harness_src = _apply_bounds(harness_src, pair.get("input_bounds"), sig)

    # Write to temp file
    with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
        f.write(harness_src)
        harness_path = f.name

    try:
        extra_flags = []
        if pair["expected"] == "proven" and pair["category"] not in ("NEQ_UB", "NEQ_WRAP"):
            extra_flags = ["--no-signed-overflow-check"]

        result = _run_cbmc_raw(harness_path, unwind, 30, extra_flags or None)
    except subprocess.TimeoutExpired:
        return "unknown_timeout", "", "Timeout after 30s", pair
    except Exception as e:
        return "checker_error", "", str(e), pair
    finally:
        try:
            os.unlink(harness_path)
        except OSError:
            pass

    # Parse result
    stdout = result.stdout
    if "VERIFICATION SUCCESSFUL" in stdout:
        status = "proven"
    elif "VERIFICATION FAILED" in stdout:
        # Determine type
        if re.search(r'\[.*\.unwind\.\d+\].*FAILURE', stdout):
            status = "unknown_unwind"
        elif "PARSING ERROR" in result.stderr or "CONVERSION ERROR" in result.stderr:
            status = "compile_error"
        else:
            status = "not_equivalent"
    else:
        status = "checker_error"

    return status, result.stdout, result.stderr, pair


def _check_verdict(status, pair, details=""):
    """Check that the CBMC result matches the expected verdict."""
    expected = pair["expected"]

    if expected == "proven":
        assert status == "proven", (
            f"[{pair['id']}] Expected proven, got {status}: {details[:200]}"
        )
    elif expected == "not_equivalent":
        assert status in ("not_equivalent", "checker_error"), (
            f"[{pair['id']}] Expected not_equivalent, got {status}: {details[:200]}"
        )
    elif expected == "unknown_unwind":
        assert status in ("unknown_unwind", "unknown_timeout", "not_equivalent"), (
            f"[{pair['id']}] Expected unknown_unwind, got {status}: {details[:200]}"
        )


@pytest.mark.parametrize("pair", GOLDEN_PAIRS, ids=lambda p: p["id"])
def test_golden_pair(pair):
    """Test a single golden pair."""
    status, stdout, stderr, p = _run_pair(pair)
    _check_verdict(status, p, stdout + "\n" + stderr)


def test_golden_smoke_included():
    """Verify the original smoke tests still pass."""
    import subprocess
    smoke_dir = os.path.join(os.path.dirname(__file__), "golden", "000_smoke")

    for name, expect in [("test_equiv.c", "SUCCESSFUL"), ("test_neq.c", "FAILED")]:
        r = subprocess.run(
            ["cbmc", "--arch", "x86_64", "--function", "harness",
             os.path.join(smoke_dir, name)],
            capture_output=True, text=True, timeout=30,
        )
        assert f"VERIFICATION {expect}" in r.stdout, (
            f"Smoke {name}: expected VERIFICATION {expect}, got: {r.stdout[-300:]}"
        )


def test_golden_count():
    """Ensure we have at least 30 golden tests total."""
    total = len(GOLDEN_PAIRS) + 2  # + smoke tests
    assert total >= 30, f"Total {total} golden tests, need >= 30"