#!/usr/bin/env python3
"""Probe: can CBMC prove correct rewrites that REORDER a reduction?

Reordering (reversed loop, k accumulators) is how unrolling/vectorization gain
speed. Stage 0.5 testing showed CBMC timing out on reordered int sums. This
script measures it on controlled kernels x rewrites x checker configs.

Usage: python scripts/probe_reorder.py --workers 2 --timeout 60 --json-out data/stage05/reorder_probe.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests"))

# kernel: (C type of accumulator/elements, params, init, element expr E(i), combine(acc, x), buf_range)
KERNELS = {
    "sum_int":   ("int", "int *a, int n", "0", "a[{i}]", "{acc} + {x}", [-32768, 32767]),
    "sum_uint":  ("unsigned", "unsigned *a, int n", "0u", "a[{i}]", "{acc} + {x}", None),
    "sumsq_int": ("int", "int *a, int n", "0", "a[{i}] * a[{i}]", "{acc} + {x}", [-1000, 1000]),
    "dot_int":   ("int", "int *a, int *b, int n", "0", "a[{i}] * b[{i}]", "{acc} + {x}", [-1000, 1000]),
    "max_int":   ("int", "int *a, int n", "-2147483647 - 1", "a[{i}]", "({x}) > ({acc}) ? ({x}) : ({acc})", None),
    "xor_uint":  ("unsigned", "unsigned *a, int n", "0u", "a[{i}]", "{acc} ^ {x}", None),
}


def _upd(comb, acc, x):
    return f"{acc} = {comb.format(acc=acc, x=x)};"


def make_variants(name: str) -> dict[str, str]:
    t, params, init, E, comb, _ = KERNELS[name]
    e = lambda i: E.format(i=i)
    f = f"{t} k_{name}({params})"
    v = {}
    v["orig"] = f"{f} {{ {t} s = {init}; for (int i = 0; i < n; i++) {_upd(comb, 's', e('i'))} return s; }}"
    v["reversed"] = f"{f} {{ {t} s = {init}; for (int i = n - 1; i >= 0; i--) {_upd(comb, 's', e('i'))} return s; }}"
    v["acc2"] = (
        f"{f} {{ {t} s0 = {init}, s1 = {init}; int i = 0;"
        f" for (; i + 1 < n; i += 2) {{ {_upd(comb, 's0', e('i'))} {_upd(comb, 's1', e('i + 1'))} }}"
        f" for (; i < n; i++) {_upd(comb, 's0', e('i'))}"
        f" return {comb.format(acc='s0', x='s1')}; }}"
    )
    v["acc4"] = (
        f"{f} {{ {t} s0 = {init}, s1 = {init}, s2 = {init}, s3 = {init}; int i = 0;"
        f" for (; i + 3 < n; i += 4) {{ {_upd(comb, 's0', e('i'))} {_upd(comb, 's1', e('i + 1'))}"
        f" {_upd(comb, 's2', e('i + 2'))} {_upd(comb, 's3', e('i + 3'))} }}"
        f" for (; i < n; i++) {_upd(comb, 's0', e('i'))}"
        f" {t} l = {comb.format(acc='s0', x='s1')}; {t} r = {comb.format(acc='s2', x='s3')};"
        f" return {comb.format(acc='l', x='r')}; }}"
    )
    # Control: unrolled by 4 but SAME order of operations.
    v["unroll4_same_order"] = (
        f"{f} {{ {t} s = {init}; int i = 0;"
        f" for (; i + 3 < n; i += 4) {{ {_upd(comb, 's', e('i'))} {_upd(comb, 's', e('i + 1'))}"
        f" {_upd(comb, 's', e('i + 2'))} {_upd(comb, 's', e('i + 3'))} }}"
        f" for (; i < n; i++) {_upd(comb, 's', e('i'))} return s; }}"
    )
    return v


CONFIGS = {
    "default_maxn8": dict(maxn=8, flags=None),
    "maxn6": dict(maxn=6, flags=None),
    "maxn4": dict(maxn=4, flags=None),
    "maxn8_slice": dict(maxn=8, flags=["--slice-formula"]),
    "maxn8_z3": dict(maxn=8, flags=["--z3"]),
}


def _task_for(name: str):
    from test_reward_pipeline import _task
    t, params, *_rest, buf_range = KERNELS[name]
    src = make_variants(name)["orig"]
    ranges = {"_narrowed": True, "_buf_range": buf_range} if buf_range else None
    return _task(f"{t} k_{name}({params})", src, f"k_{name}", input_ranges=ranges)


def run_one(args):
    kernel, variant, cfg_name, timeout = args
    from equiv.verify import verify
    cfg = CONFIGS[cfg_name]
    task = _task_for(kernel)
    cand = make_variants(kernel)[variant]
    start = time.monotonic()
    try:
        r = verify(task, cand, timeout_s=timeout, maxn=cfg["maxn"], unwind=cfg["maxn"] + 2,
                   extra_cbmc_flags=cfg["flags"])
        status, details = r.status, r.details[:160]
    except Exception as e:  # noqa: BLE001
        status, details = "error", str(e)[:160]
    return {"kernel": kernel, "variant": variant, "config": cfg_name, "status": status,
            "time_s": round(time.monotonic() - start, 2), "details": details}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--timeout", type=float, default=60)
    p.add_argument("--kernels", nargs="*", default=list(KERNELS))
    p.add_argument("--configs", nargs="*", default=list(CONFIGS))
    p.add_argument("--json-out", default=None)
    a = p.parse_args()

    variants = ["reversed", "acc2", "acc4", "unroll4_same_order"]
    jobs = [(k, v, c, a.timeout) for c in a.configs for k in a.kernels for v in variants]
    rows = []
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(run_one, j) for j in jobs]
        for f in as_completed(futs):
            r = f.result()
            rows.append(r)
            print(f"{r['config']:14s} {r['kernel']:10s} {r['variant']:19s} {r['status']:16s} {r['time_s']:6.1f}s",
                  flush=True)
    if a.json_out:
        with open(a.json_out, "w") as fh:
            json.dump(rows, fh, indent=1)


if __name__ == "__main__":
    main()
