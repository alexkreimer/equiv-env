#!/usr/bin/env python3
"""Validate speed proxies on real equivalent-but-faster source-level pairs.

Compares:
  - inproc_time run 1 vs run 2 (stability / repeatability Spearman)
  - static_asm vs inproc_time  (comparison row, per Review 2 Task F)
  - callgrind vs inproc_time   (when valgrind is available, e.g. Linux / Docker)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from equiv.extract import _check_eligibility
from equiv.speed import (
    have_valgrind,
    machine_fingerprint,
    measure_speedup,
    spearman,
)
from tests.golden.catalog import GOLDEN_PAIRS

# Extra loop/buffer pairs spanning 1x..50x real speedups (complementing scalar golden pairs)
EXTRA_LOOP_PAIRS = [
    {
        "id": "loop_prefix_quad_to_lin",
        "original": (
            "int prefix_total(int *a, int n) {\n"
            "  int s = 0;\n"
            "  for (int i = 0; i < n; i++)\n"
            "    for (int j = 0; j <= i; j++) s += a[j];\n"
            "  return s;\n"
            "}"
        ),
        "candidate": (
            "int prefix_total(int *a, int n) {\n"
            "  int s = 0, run = 0;\n"
            "  for (int i = 0; i < n; i++) { run += a[i]; s += run; }\n"
            "  return s;\n"
            "}"
        ),
    },
    {
        "id": "loop_redundant_pass",
        "original": (
            "int sum_twice(int *a, int n) {\n"
            "  int s1 = 0, s2 = 0;\n"
            "  for (int i = 0; i < n; i++) s1 += a[i];\n"
            "  for (int i = 0; i < n; i++) s2 += a[i];\n"
            "  return s1 + s2;\n"
            "}"
        ),
        "candidate": (
            "int sum_twice(int *a, int n) {\n"
            "  int s = 0;\n"
            "  for (int i = 0; i < n; i++) s += a[i];\n"
            "  return s * 2;\n"
            "}"
        ),
    },
    {
        "id": "loop_hoist_invariant",
        "original": (
            "int weighted_sum(int *a, int n, int k) {\n"
            "  int s = 0;\n"
            "  for (int i = 0; i < n; i++) {\n"
            "    int w = 0;\n"
            "    for (int j = 0; j < k; j++) w += j;\n"
            "    s += a[i] * w;\n"
            "  }\n"
            "  return s;\n"
            "}"
        ),
        "candidate": (
            "int weighted_sum(int *a, int n, int k) {\n"
            "  int w = k * (k - 1) / 2, s = 0;\n"
            "  for (int i = 0; i < n; i++) s += a[i];\n"
            "  return s * w;\n"
            "}"
        ),
    },
    {
        "id": "loop_identity_baseline",
        "original": "int arr_sum(int *a, int n) { int s = 0; for (int i = 0; i < n; i++) s += a[i]; return s; }",
        "candidate": "int arr_sum(int *a, int n) { int s = 0; for (int i = 0; i < n; i++) s = s + a[i]; return s; }",
    },
    {
        "id": "loop_linear_vs_quad_max_diff",
        "original": (
            "int max_diff(int *a, int n) {\n"
            "  int best = 0;\n"
            "  for (int i = 0; i < n; i++)\n"
            "    for (int j = 0; j < n; j++) {\n"
            "      int d = a[i] - a[j];\n"
            "      if (d > best) best = d;\n"
            "    }\n"
            "  return best;\n"
            "}"
        ),
        "candidate": (
            "int max_diff(int *a, int n) {\n"
            "  if (n <= 0) return 0;\n"
            "  int mn = a[0], mx = a[0];\n"
            "  for (int i = 1; i < n; i++) {\n"
            "    if (a[i] < mn) mn = a[i];\n"
            "    if (a[i] > mx) mx = a[i];\n"
            "  }\n"
            "  return mx - mn;\n"
            "}"
        ),
    },
    {
        "id": "loop_div_to_shift",
        "original": "int div8_sum(unsigned int *a, int n) { unsigned int s = 0; for (int i = 0; i < n; i++) s += a[i] / 8u; return (int)s; }",
        "candidate": "int div8_sum(unsigned int *a, int n) { unsigned int s = 0; for (int i = 0; i < n; i++) s += a[i] >> 3; return (int)s; }",
    },
]


def _make_task(pair: dict):
    orig = pair["original"]
    # Extract function name from header
    import re
    m = re.search(r'\b([A-Za-z_]\w*)\s*\(', orig.split("{")[0])
    fname = m.group(1) if m else "f"
    rec = {
        "text": {
            "func_head": "",
            "func_def": orig,
            "fname": fname,
            "path": f"bench/{pair['id']}.c",
            "real_error": "",
            "angha_deps": "",
        }
    }
    elig = _check_eligibility(rec, "test")
    return elig.task if elig.eligible else None


def main() -> int:
    print(f"Machine: {machine_fingerprint()}")
    use_cg = have_valgrind()
    print(f"Valgrind/callgrind available: {use_cg}\n")

    pairs = [p for p in GOLDEN_PAIRS if p["category"] == "EQ_FASTER" and p["expected"] == "proven"]
    pairs.extend(EXTRA_LOOP_PAIRS)

    t1_list, t2_list, asm_list, cg_list = [], [], [], []
    for p in pairs:
        task = _make_task(p)
        if task is None:
            print(f"  {p['id']:28s}: SKIP (not eligible)")
            continue
        r1 = measure_speedup(task, p["candidate"], proxy="inproc_time")
        r2 = measure_speedup(task, p["candidate"], proxy="inproc_time")
        ra = measure_speedup(task, p["candidate"], proxy="static_asm")
        if r1.error or r2.error or ra.error:
            print(f"  {p['id']:28s}: SKIP ({r1.error or r2.error or ra.error})")
            continue
        if not r1.outputs_match:
            print(f"  {p['id']:28s}: CHECKSUM MISMATCH (large-input divergence!)")
            continue

        t1_list.append(r1.speedup)
        t2_list.append(r2.speedup)
        asm_list.append(ra.speedup)

        cg_str = ""
        if use_cg:
            rc = measure_speedup(task, p["candidate"], proxy="callgrind")
            if not rc.error and rc.outputs_match:
                cg_list.append((r1.speedup, rc.speedup))
                cg_str = f"  cg={rc.speedup:6.2f}x"

        print(
            f"  {p['id']:28s}: inproc={r1.speedup:6.2f}x / {r2.speedup:6.2f}x "
            f"({r1.orig_value:7.1f}->{r1.cand_value:6.1f}ns)  asm={ra.speedup:5.2f}x{cg_str}"
        )

    print(f"\nValid pairs: {len(t1_list)}")
    rho_rep = spearman(t1_list, t2_list)
    rho_asm = spearman(asm_list, t1_list)
    print(f"inproc_time repeatability Spearman (run1 vs run2): {rho_rep:.3f}")
    print(f"static_asm vs inproc_time Spearman:                {rho_asm:.3f}")
    if cg_list:
        rho_cg = spearman([c for _, c in cg_list], [t for t, _ in cg_list])
        print(f"callgrind  vs inproc_time Spearman (n={len(cg_list)}):       {rho_cg:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
