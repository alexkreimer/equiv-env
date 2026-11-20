#!/usr/bin/env python3
"""Speed proxy: static asm vs wall-clock on scalar EQ_FASTER golden pairs."""

import sys, os, time, tempfile, subprocess, statistics, re

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests.golden.catalog import GOLDEN_PAIRS

EQ_FASTER = [p for p in GOLDEN_PAIRS if p["category"] == "EQ_FASTER"]

# Only scalar int-param pairs (no pointers)
scalar = []
for p in EQ_FASTER:
    src = p["original"]
    has_ptr = "*" in src.split("(")[0] or "arr" in src
    if not has_ptr:
        scalar.append(p)

print(f"Testing {len(scalar)} scalar pairs\n")


def count_asm(src: str) -> int:
    with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
        f.write(src)
        sp = f.name
    try:
        r = subprocess.run(["gcc", "-O2", "-fsigned-char", "-S", "-o", "-", sp],
                          capture_output=True, text=True, timeout=10)
        lines = r.stdout.split("\n")
        return len([l for l in lines if l.strip() and "\t" in l and not l.startswith("\t.")])
    except:
        return -1
    finally:
        os.unlink(sp)


def extract_name(src: str) -> str:
    m = re.search(r'\b(\w+)\s*\(', src)
    return m.group(1) if m else "f"


def wallclock(src: str, n: int = 500_000) -> float:
    name = extract_name(src)
    if name in ("memset", "memcmp"):
        return -1
    # Detect arity from signature
    sig_match = re.search(r'\((.*?)\)', src.split("{")[0] if "{" in src else src)
    n_params = 0
    if sig_match:
        params = sig_match.group(1).strip()
        if params and params != "void":
            n_params = len([p for p in params.split(",") if p.strip()])
    arg = f"(i, i + 1)" if n_params >= 2 else "(i)"
    driver = src + f"""
int main(void) {{
    volatile int r = 0;
    for (int i = 0; i < {n}; i++) {{
        r += {name}{arg};
    }}
    return r;
}}
"""
    with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
        f.write(driver)
        sp = f.name
    try:
        subprocess.run(["gcc", "-O2", "-fsigned-char", "-o", sp + ".out", sp],
                      capture_output=True, timeout=10, check=True)
        times = []
        for _ in range(3):
            t0 = time.perf_counter()
            subprocess.run([sp + ".out"], capture_output=True, timeout=10)
            times.append((time.perf_counter() - t0) / n * 1e9)
        return statistics.median(times)
    except:
        return -1
    finally:
        for p in [sp, sp + ".out"]:
            try: os.unlink(p)
            except OSError: pass


def spearman(xs, ys):
    n = len(xs)
    if n < 2: return 0.0
    def rank(vs):
        s = sorted(set(vs))
        return [s.index(v) for v in vs]
    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx)/n, sum(ry)/n
    num = sum((rx[i]-mx)*(ry[i]-my) for i in range(n))
    dx = sum((x-mx)**2 for x in rx)
    dy = sum((y-my)**2 for y in ry)
    if dx == 0 or dy == 0: return 0.0
    return num / (dx**0.5 * dy**0.5)


results = []
for p in scalar:
    pid = p["id"]
    oa, ca = count_asm(p["original"]), count_asm(p["candidate"])
    ow, cw = wallclock(p["original"]), wallclock(p["candidate"])
    if oa > 0 and ca > 0 and ow > 0 and cw > 0:
        asp = round(oa/ca, 2)
        wsp = round(ow/cw, 2)
        results.append((pid, asp, wsp))
        print(f"  {pid}: asm {oa}→{ca} ({asp}x)  wall {ow:.0f}→{cw:.0f}ns ({wsp}x)")
    else:
        print(f"  {pid}: SKIP (asm={oa}/{ca} wall={ow}/{cw})")

if len(results) >= 2:
    asm_sp = [r[1] for r in results]
    wc_sp = [r[2] for r in results]
    rho = spearman(asm_sp, wc_sp)
    print(f"\nSpearman: {rho:.3f} (n={len(results)})")
    print(f"Target ≥0.8: {'PASS' if rho >= 0.8 else 'NOT MET'}")
else:
    print(f"\nNeed ≥2 valid points, got {len(results)}")