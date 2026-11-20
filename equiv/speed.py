"""
speed.py — Speed proxy for (original, candidate) C functions.

Design (fixes for the previous version):
  * The function under test is compiled in its OWN translation unit and called
    from a separate driver. Previously both lived in one file, so at -O3 the
    compiler could inline the call, hoist it out of the loop (constant args) or
    constant-fold it, and the "benchmark" measured nothing.
  * The driver varies the scalar arguments on every call and uses large buffers
    (BENCH_N elements) so loop trip counts matter.
  * Timing happens INSIDE the process (clock_gettime around a batch of calls),
    not around subprocess.run — process spawn (~ms) used to swamp 30 ns calls.
  * The driver also prints a checksum of every return value and of the final
    buffer contents. Original and candidate must agree on it: this is a cheap
    large-input differential check, because CBMC only proves equivalence up to
    MAXN elements (a candidate could special-case n > MAXN).
  * Proxies:  "inproc_time"  best-of-reps ns/call, in-process     (any platform)
              "callgrind"    instructions/call, `--toggle-collect` (needs valgrind)
              "static_asm"   static instruction count             (comparison only)
    Which proxy ran, on which machine, with which flags is returned so numbers
    are never compared across machines (Review 2, C3).

Usage:
    from equiv.speed import measure_speedup
    r = measure_speedup(task, candidate_src, proxy="inproc_time")
    r.speedup, r.outputs_match
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import statistics
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Literal

from .extract import Task
from .harness import (
    _clean_type_str,
    _filter_deps,
    _header_for_task,
    _pointee_type,
    _rename_function,
)

ProxyKind = Literal["inproc_time", "callgrind", "static_asm"]

BENCH_N = 512            # elements per buffer / value of length-like params
FUT = "fut__under_test"  # renamed function under test (avoids libc symbol clashes)


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------

def default_arch() -> str | None:
    """Pinned -march for this machine (None = compiler default)."""
    m = platform.machine().lower()
    if m in ("arm64", "aarch64"):
        return "armv8-a"
    if m in ("x86_64", "amd64"):
        return "x86-64-v2"
    return None


def machine_fingerprint(arch: str | None = None) -> dict:
    """Recorded with every measurement: speedups do not transfer across machines."""
    return {"machine": platform.machine(), "system": platform.system(),
            "arch_flag": arch if arch is not None else default_arch(),
            "bench_n": BENCH_N}


def is_emulated() -> bool:
    """True if this process runs under Rosetta/QEMU (speed numbers would be invalid)."""
    if platform.system() == "Darwin":
        try:
            out = subprocess.run(["sysctl", "-n", "sysctl.proc_translated"],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            return out == "1"
        except Exception:
            return False
    return False


def have_valgrind() -> bool:
    return shutil.which("valgrind") is not None


@dataclass
class SpeedResult:
    speedup: float = 1.0                 # proxy(original) / proxy(candidate); > 1 = candidate faster
    orig_value: float | None = None      # proxy units (ns/call or instructions/call)
    cand_value: float | None = None
    proxy: str = ""
    outputs_match: bool = True           # large-input differential check
    error: str | None = None             # set when the measurement could not be made
    machine: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Driver generation
# --------------------------------------------------------------------------

_STORAGE_RE = re.compile(r'\b(?:static|inline|__inline__|__inline|extern)\b')


def _external_function_src(task: Task, func_src: str, name: str, symbol: str = FUT) -> str | None:
    """Rename to `symbol` and strip storage/inline specifiers so it links from another TU."""
    brace = func_src.find("{")
    if brace == -1:
        return None
    head, body = func_src[:brace], func_src[brace:]
    head = _STORAGE_RE.sub(" ", head)
    return _rename_function(head + body, name, symbol)


def _extract_cand_name(task: Task, candidate_src: str) -> str:
    from .harness import _extract_function_name
    return _extract_function_name(candidate_src) or task.func_name


def _build_paired_inproc_sources(
    task: Task, candidate_src: str, *, calls: int, reps: int
) -> tuple[str, str, str] | None:
    """Return (orig_tu, cand_tu, interleaved_driver_tu) for inproc_time.

    Running orig and cand interleaved inside one process (while keeping them in
    separate TUs to prevent cross-TU inlining) eliminates P-core vs E-core
    scheduling skew between separate subprocesses.
    """
    sig = task.signature
    orig_body = _external_function_src(task, task.func_src, task.func_name, "fut__orig")
    cand_body = _external_function_src(task, candidate_src, _extract_cand_name(task, candidate_src), "fut__cand")
    if orig_body is None or cand_body is None:
        return None

    deps = _filter_deps(task.deps_src) if task.deps_src else ""
    header = _header_for_task(task)
    orig_tu = "\n".join([header, deps, orig_body, ""])
    cand_tu = "\n".join([header, deps, cand_body, ""])

    ret = _clean_type_str(sig.return_type)
    lengths = _length_params(task)
    pointer_rules = task.pointer_rules or {}
    proto_params = ", ".join(f"{_clean_type_str(t)} {n}" for n, t in sig.params) or "void"

    d = [
        header, deps, "#include <stdio.h>", "#include <time.h>", "#include <stdint.h>",
        f"#define BENCH_N {BENCH_N}", f"#define CALLS {calls}", f"#define REPS {reps}",
        f"{ret} fut__orig({proto_params});",
        f"{ret} fut__cand({proto_params});", "",
    ]
    ptr_params = [(n, t) for n, t in sig.params if _is_ptr(t)]
    for n, t in ptr_params:
        d.append(f"static {_pointee_type(t)} {n}_buf[BENCH_N];")
    d.append("static void init_buffers(void) {")
    for i, (n, t) in enumerate(ptr_params):
        p = _pointee_type(t)
        if pointer_rules.get(n) == "string":
            d.append(f"  for (int i = 0; i < BENCH_N; i++) {n}_buf[i] = ({p})(33 + (i * 7 + {i}) % 90);")
            d.append(f"  {n}_buf[BENCH_N - 1] = 0;")
        else:
            d.append(f"  for (int i = 0; i < BENCH_N; i++) {n}_buf[i] = ({p})((i * 7 + {i * 3 + 1}) % 100);")
    d.append("}")
    d.append("static uint64_t checksum_buffers(void) {")
    d.append("  uint64_t h = 1469598103934665603ULL;")
    for n, t in ptr_params:
        d.append(f"  for (int i = 0; i < BENCH_N; i++) h = (h ^ (uint64_t)(int64_t){n}_buf[i]) * 1099511628211ULL;")
    d.append("  return h;\n}")

    args = []
    for idx, (n, t) in enumerate(sig.params):
        if _is_ptr(t):
            args.append(f"{n}_buf")
        elif n in lengths:
            args.append(f"({_clean_type_str(t)})BENCH_N")
        else:
            args.append(f"({_clean_type_str(t)})(((k * 2654435761u) >> {8 + idx}) % 29 + {idx + 1})")
    arg_str = ", ".join(args)

    def _emit_run(sym: str, out_ns: str, out_cs: str):
        d.append("    init_buffers();")
        d.append("    { struct timespec t0, t1; uint64_t acc = 0;")
        d.append("      clock_gettime(CLOCK_MONOTONIC, &t0);")
        d.append("      for (unsigned k = 0; k < CALLS; k++) {")
        if ret == "void":
            d.append(f"        {sym}({arg_str});")
        elif ret.endswith("*"):
            d.append(f"        acc += (uint64_t)({sym}({arg_str}) != 0);")
        else:
            d.append(f"        acc += (uint64_t)(int64_t){sym}({arg_str});")
        d.append('        __asm__ volatile("" ::: "memory");')
        d.append("      }")
        d.append("      clock_gettime(CLOCK_MONOTONIC, &t1);")
        d.append(f"      double ns = ((t1.tv_sec - t0.tv_sec) * 1e9 + (t1.tv_nsec - t0.tv_nsec)) / CALLS;")
        d.append(f"      if (rep >= 2 && ns < {out_ns}) {out_ns} = ns;")
        d.append(f"      sink += acc;")
        d.append(f"      {out_cs} = ({out_cs} ^ acc) * 1099511628211ULL; {out_cs} = ({out_cs} ^ checksum_buffers()) * 1099511628211ULL;")
        d.append("    }")

    d.append("int main(void) {")
    d.append("  volatile uint64_t sink = 0;")
    d.append("  uint64_t cs_o = 1469598103934665603ULL, cs_c = 1469598103934665603ULL;")
    d.append("  double best_o = 1e30, best_c = 1e30;")
    d.append("  for (int rep = 0; rep < REPS + 2; rep++) {")
    _emit_run("fut__orig", "best_o", "cs_o")
    _emit_run("fut__cand", "best_c", "cs_c")
    d.append("  }")
    d.append('  printf("ORIG_NS %.6f\\nCAND_NS %.6f\\nMATCH %d\\n", best_o, best_c, cs_o == cs_c ? 1 : 0);')
    d.append("  return 0;\n}")
    return orig_tu, cand_tu, "\n".join(d)


def _is_ptr(c_type: str) -> bool:
    return c_type.strip().endswith("*")


def _length_params(task: Task) -> set[str]:
    from .extract import get_paired_length_params
    return get_paired_length_params(task.signature, task.pointer_rules)


def build_speed_sources(task: Task, func_src: str, *, calls: int, reps: int) -> tuple[str, str] | None:
    """Return (function_tu_source, driver_tu_source), or None if not buildable."""
    sig = task.signature
    func_tu_body = _external_function_src(task, func_src, task.func_name)
    if func_tu_body is None:
        return None

    deps = _filter_deps(task.deps_src) if task.deps_src else ""
    header = _header_for_task(task)

    func_tu = "\n".join([header, deps, func_tu_body, ""])

    ret = _clean_type_str(sig.return_type)
    lengths = _length_params(task)
    pointer_rules = task.pointer_rules or {}

    proto_params = ", ".join(f"{_clean_type_str(t)} {n}" for n, t in sig.params) or "void"
    proto = f"{ret} {FUT}({proto_params});"

    d = [header, deps, "#include <stdio.h>", "#include <time.h>", "#include <stdint.h>",
         f"#define BENCH_N {BENCH_N}", f"#define CALLS {calls}", f"#define REPS {reps}", proto, ""]

    # Buffers + initialisers (deterministic, small values: avoid arithmetic overflow)
    ptr_params = [(n, t) for n, t in sig.params if _is_ptr(t)]
    for n, t in ptr_params:
        d.append(f"static {_pointee_type(t)} {n}_buf[BENCH_N];")
    d.append("static void init_buffers(void) {")
    for i, (n, t) in enumerate(ptr_params):
        p = _pointee_type(t)
        if pointer_rules.get(n) == "string":
            # strings: no zero byte until the terminator
            d.append(f"  for (int i = 0; i < BENCH_N; i++) {n}_buf[i] = ({p})(33 + (i * 7 + {i}) % 90);")
            d.append(f"  {n}_buf[BENCH_N - 1] = 0;")
        else:
            d.append(f"  for (int i = 0; i < BENCH_N; i++) {n}_buf[i] = ({p})((i * 7 + {i * 3 + 1}) % 100);")
    d.append("}")
    d.append("static uint64_t checksum_buffers(void) {")
    d.append("  uint64_t h = 1469598103934665603ULL;")
    for n, t in ptr_params:
        d.append(f"  for (int i = 0; i < BENCH_N; i++) h = (h ^ (uint64_t)(int64_t){n}_buf[i]) * 1099511628211ULL;")
    d.append("  return h;\n}")

    # scalar args vary with k so the call cannot be hoisted out of the loop
    args = []
    for idx, (n, t) in enumerate(sig.params):
        if _is_ptr(t):
            args.append(f"{n}_buf")
        elif n in lengths:
            args.append(f"({_clean_type_str(t)})BENCH_N")
        else:
            args.append(f"({_clean_type_str(t)})(((k * 2654435761u) >> {8 + idx}) % 29 + {idx + 1})")
    call = f"{FUT}({', '.join(args)})"

    d.append("int main(void) {")
    d.append("  volatile uint64_t sink = 0; uint64_t cs = 1469598103934665603ULL;")
    d.append("  double best = 1e30;")
    d.append("  for (int rep = 0; rep < REPS; rep++) {")
    d.append("    init_buffers();")
    d.append("    struct timespec t0, t1; clock_gettime(CLOCK_MONOTONIC, &t0);")
    d.append("    uint64_t acc = 0;")
    d.append("    for (unsigned k = 0; k < CALLS; k++) {")
    if ret == "void":
        d.append(f"      {call};")
    elif ret.endswith("*"):
        d.append(f"      acc += (uint64_t)({call} != 0);")
    else:
        d.append(f"      acc += (uint64_t)(int64_t){call};")
    d.append('      __asm__ volatile("" ::: "memory");')
    d.append("    }")
    d.append("    clock_gettime(CLOCK_MONOTONIC, &t1);")
    d.append("    double ns = ((t1.tv_sec - t0.tv_sec) * 1e9 + (t1.tv_nsec - t0.tv_nsec)) / CALLS;")
    d.append("    if (ns < best) best = ns;")
    d.append("    sink += acc;")
    d.append("    cs = (cs ^ acc) * 1099511628211ULL; cs = (cs ^ checksum_buffers()) * 1099511628211ULL;")
    d.append("  }")
    d.append('  printf("BEST_NS %.6f\\nCHECKSUM %llu\\n", best, (unsigned long long)cs);')
    d.append("  return 0;\n}")
    return func_tu, "\n".join(d)


# --------------------------------------------------------------------------
# Building and measuring
# --------------------------------------------------------------------------

def _build(func_tu: str, driver_tu: str, workdir: str, arch: str | None) -> str | None:
    """Compile both TUs separately (no LTO => no cross-TU inlining) and link."""
    fpath = os.path.join(workdir, "func.c")
    dpath = os.path.join(workdir, "driver.c")
    with open(fpath, "w") as f:
        f.write(func_tu)
    with open(dpath, "w") as f:
        f.write(driver_tu)
    flags = ["-O3", "-fsigned-char", "-fno-builtin", "-w"]
    if arch:
        flags.append(f"-march={arch}")
    objs = []
    for src in (fpath, dpath):
        obj = src[:-2] + ".o"
        r = subprocess.run(["gcc", *flags, "-c", "-o", obj, src], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return None
        objs.append(obj)
    exe = os.path.join(workdir, "bench")
    r = subprocess.run(["gcc", "-o", exe, *objs], capture_output=True, text=True, timeout=60)
    return exe if r.returncode == 0 else None


def _run_inproc(exe: str, timeout_s: float = 120) -> tuple[float, str] | None:
    try:
        r = subprocess.run([exe], capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    m_t = re.search(r"BEST_NS\s+([\d.]+)", r.stdout)
    m_c = re.search(r"CHECKSUM\s+(\d+)", r.stdout)
    if not (m_t and m_c):
        return None
    return float(m_t.group(1)), m_c.group(1)


def _run_callgrind(exe: str, calls: int, reps: int, timeout_s: float = 600) -> tuple[float, str] | None:
    """Instructions/call from callgrind, counting only the function under test."""
    out = exe + ".cg"
    try:
        r = subprocess.run(
            ["valgrind", "--tool=callgrind", f"--toggle-collect={FUT}",
             f"--callgrind-out-file={out}", exe],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if r.returncode != 0:
        return None
    m_c = re.search(r"CHECKSUM\s+(\d+)", r.stdout)
    total = None
    try:
        with open(out) as f:
            for line in f:
                if line.startswith(("summary:", "totals:")):
                    total = int(line.split(":", 1)[1].split()[0])
                    break
    except OSError:
        return None
    if total is None or not m_c:
        return None
    return total / float(calls * reps), m_c.group(1)


def _static_asm(func_tu: str, workdir: str, arch: str | None) -> float | None:
    src = os.path.join(workdir, "static.c")
    with open(src, "w") as f:
        f.write(func_tu)
    flags = ["-O3", "-fsigned-char", "-fno-builtin", "-w"] + ([f"-march={arch}"] if arch else [])
    r = subprocess.run(["gcc", *flags, "-S", "-o", "-", src], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return None
    n = 0
    for line in r.stdout.splitlines():
        s = line.strip()
        if not s or s.startswith(('.', '//', '#', '"', ';')) or s.endswith(':'):
            continue
        n += 1
    return float(n)


def measure_value(task: Task, func_src: str, proxy: ProxyKind, *, arch: str | None = None) -> tuple[float, str] | None:
    """Measure one function. Returns (proxy value, output checksum) or None."""
    arch = arch if arch is not None else default_arch()
    has_buf = any(_is_ptr(t) for _, t in task.signature.params)
    if proxy == "callgrind":
        calls, reps = 200, 1
    else:
        calls = max(1000, 3_000_000 // BENCH_N) if has_buf else 2_000_000
        reps = 9
    srcs = build_speed_sources(task, func_src, calls=calls, reps=reps)
    if srcs is None:
        return None
    func_tu, driver_tu = srcs
    wd = tempfile.mkdtemp(prefix="equiv_speed_")
    try:
        if proxy == "static_asm":
            v = _static_asm(func_tu, wd, arch)
            return (v, "") if v is not None else None
        exe = _build(func_tu, driver_tu, wd, arch)
        if exe is None:
            return None
        if proxy == "callgrind":
            return _run_callgrind(exe, calls, reps)
        return _run_inproc(exe)
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def _measure_paired_inproc(
    task: Task, candidate_src: str, *, arch: str | None
) -> tuple[float, float, bool] | None:
    """Compile orig.o + cand.o + driver.o separately, link, and run interleaved."""
    arch = arch if arch is not None else default_arch()
    has_buf = any(_is_ptr(t) for _, t in task.signature.params)
    calls = max(1000, 3_000_000 // BENCH_N) if has_buf else 2_000_000
    srcs = _build_paired_inproc_sources(task, candidate_src, calls=calls, reps=9)
    if srcs is None:
        return None
    orig_tu, cand_tu, driver_tu = srcs
    wd = tempfile.mkdtemp(prefix="equiv_speed_")
    try:
        flags = ["-O3", "-fsigned-char", "-fno-builtin", "-w"]
        if arch:
            flags.append(f"-march={arch}")
        objs = []
        for fname, content in (("orig.c", orig_tu), ("cand.c", cand_tu), ("driver.c", driver_tu)):
            sp = os.path.join(wd, fname)
            op = sp[:-2] + ".o"
            with open(sp, "w") as f:
                f.write(content)
            r = subprocess.run(["gcc", *flags, "-c", "-o", op, sp], capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                return None
            objs.append(op)
        exe = os.path.join(wd, "bench")
        r = subprocess.run(["gcc", "-o", exe, *objs], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return None
        try:
            run = subprocess.run([exe], capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return None
        if run.returncode != 0:
            return None
        mo = re.search(r"ORIG_NS\s+([\d.]+)", run.stdout)
        mc = re.search(r"CAND_NS\s+([\d.]+)", run.stdout)
        mm = re.search(r"MATCH\s+(\d+)", run.stdout)
        if not (mo and mc and mm):
            return None
        return float(mo.group(1)), float(mc.group(1)), mm.group(1) == "1"
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def measure_speedup(
    task: Task,
    candidate_src: str,
    *,
    proxy: ProxyKind = "inproc_time",
    arch: str | None = None,
) -> SpeedResult:
    """Speedup of candidate over original (> 1.0 = candidate faster)."""
    res = SpeedResult(proxy=proxy, machine=machine_fingerprint(arch))
    if is_emulated():
        res.error = "refusing to measure speed under emulation (Rosetta/QEMU)"
        return res
    if proxy == "callgrind" and not have_valgrind():
        res.error = "valgrind not available (callgrind proxy needs Linux + valgrind)"
        return res

    if proxy == "inproc_time":
        paired = _measure_paired_inproc(task, candidate_src, arch=arch)
        if paired is None:
            res.error = "could not build/measure paired benchmark"
            return res
        res.orig_value, res.cand_value, res.outputs_match = paired
        if res.cand_value and res.cand_value > 0:
            res.speedup = res.orig_value / res.cand_value
        return res

    o = measure_value(task, task.func_src, proxy, arch=arch)
    c = measure_value(task, candidate_src, proxy, arch=arch)
    if o is None:
        res.error = "could not build/measure the original"
        return res
    if c is None:
        res.error = "could not build/measure the candidate"
        return res

    res.orig_value, res.cand_value = o[0], c[0]
    res.outputs_match = (o[1] == c[1]) if proxy != "static_asm" else True
    if c[0] > 0:
        res.speedup = o[0] / c[0]
    return res


def spearman(xs: list[float], ys: list[float]) -> float:
    """Spearman rank correlation with average ranks for ties."""
    n = len(xs)
    if n < 2:
        return 0.0

    def ranks(vs):
        order = sorted(range(n), key=lambda i: vs[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vs[order[j + 1]] == vs[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return num / (dx * dy) if dx and dy else 0.0
