"""
replay.py — Replay a CBMC counterexample on the real compiled functions.

A `not_equivalent` verdict is only trustworthy if the counterexample actually
reproduces when the original and candidate are compiled normally (spec §5/§7.3).
This catches harness bugs (the checker disagreeing with real C semantics).

Statuses:
  replayed        the functions really differ on the counterexample input
                  (or, for UB counterexamples, the candidate trips ASan/UBSan
                  after the original ran cleanly)
  not_reproduced  the driver compiled and ran but showed no difference
                  -> a potential checker/harness bug
  driver_error    the replay driver itself failed to compile/run
                  -> a replay-tooling problem, NOT evidence about the checker
  not_applicable  no usable counterexample
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass

from .extract import Task
from .harness import (
    _clean_type_str,
    _extract_function_name,
    _filter_deps,
    _header_for_task,
    _pointee_type,
    _rename_function,
)

ReplayStatus = str  # "replayed" | "not_reproduced" | "driver_error" | "not_applicable"

_SANITIZE_FLAGS = ["-fsanitize=undefined,address", "-fno-sanitize-recover=all", "-g"]
_INLINE_RE = re.compile(r'\b(?:inline|__inline__|__inline|extern)\b')


def _strip_inline(func_src: str) -> str:
    """Strip inline/extern from function header so -O0 always emits a symbol."""
    brace = func_src.find("{")
    if brace == -1:
        return func_src
    return _INLINE_RE.sub(" ", func_src[:brace]) + func_src[brace:]


@dataclass
class ReplayResult:
    status: ReplayStatus
    details: str = ""


def _c_value(c_type: str, value) -> str:
    """C expression for an integer counterexample value of the given type."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = 0
    lit = f"{v}ULL" if v >= (1 << 63) else f"({v}LL)"
    return f"({_clean_type_str(c_type)})({lit})"


def build_replay_driver(task: Task, candidate_src: str, ce: dict, *, maxn: int | None = None) -> str:
    """Build a C driver that runs original and candidate on the counterexample."""
    maxn = maxn or task.maxn or 8
    sig = task.signature
    inputs = ce.get("inputs") or {}
    buffers = ce.get("buffers") or {}

    orig = _strip_inline(_rename_function(task.func_src, task.func_name, "orig_func") or task.func_src)
    cand_name = _extract_function_name(candidate_src) or task.func_name
    cand = _strip_inline(_rename_function(candidate_src, cand_name, "mut_func") or candidate_src)

    parts = [_header_for_task(task)]
    parts.append(f"#define MAXN {maxn}")
    if task.deps_src:
        filtered = _filter_deps(task.deps_src)
        if filtered:
            parts.append(filtered)
    parts.append(orig)
    parts.append(cand)
    parts.append("#include <stdio.h>\nint main(void) {")

    pointer_params = []
    for name, c_type in sig.params:
        if c_type.strip().endswith("*"):
            pointee = _pointee_type(c_type)
            vals = list(buffers.get(name, []))[:maxn]
            vals += [0] * (maxn - len(vals))
            init = ", ".join(_c_value(pointee, v) for v in vals)
            parts.append(f"  {pointee} {name}_o[MAXN] = {{{init}}};")
            parts.append(f"  {pointee} {name}_m[MAXN] = {{{init}}};")
            pointer_params.append(name)
        else:
            parts.append(f"  {_clean_type_str(c_type)} {name} = {_c_value(c_type, inputs.get(name, 0))};")

    oa = [f"{n}_o" if t.strip().endswith("*") else n for n, t in sig.params]
    ma = [f"{n}_m" if t.strip().endswith("*") else n for n, t in sig.params]
    ret = _clean_type_str(sig.return_type)

    if ret == "void":
        parts.append(f"  orig_func({', '.join(oa)});")
        parts.append('  printf("ORIG_OK\\n"); fflush(stdout);')
        parts.append(f"  mut_func({', '.join(ma)});")
    else:
        parts.append(f"  {ret} r_o = orig_func({', '.join(oa)});")
        parts.append('  printf("ORIG_OK\\n"); fflush(stdout);')
        parts.append(f"  {ret} r_m = mut_func({', '.join(ma)});")
        if ret.endswith("*"):
            # Pointer results into distinct input buffers: compare by offset.
            if pointer_params:
                base = pointer_params[0]
                parts.append(
                    f"  long off_o = r_o ? (long)(r_o - {base}_o) : -1L;"
                    f"  long off_m = r_m ? (long)(r_m - {base}_m) : -1L;"
                )
                parts.append('  if (off_o != off_m) { printf("DIFFERS\\n"); return 0; }')
            else:
                # Pointer return with no buffer params (e.g. static string literal table)
                parts.append('  if ((r_o == NULL) != (r_m == NULL)) { printf("DIFFERS\\n"); return 0; }')
                if "char" in _pointee_type(ret):
                    parts.append('  if (r_o && r_m && strcmp((const char *)r_o, (const char *)r_m) != 0) { printf("DIFFERS\\n"); return 0; }')
                else:
                    parts.append('  if (r_o != r_m) { printf("DIFFERS\\n"); return 0; }')
        else:
            parts.append('  if (r_o != r_m) { printf("DIFFERS\\n"); return 0; }')

    for name in pointer_params:
        parts.append(
            f'  for (int i = 0; i < MAXN; i++) if ({name}_o[i] != {name}_m[i]) '
            f'{{ printf("DIFFERS\\n"); return 0; }}'
        )
    parts.append('  printf("SAME\\n"); return 0; }')
    return "\n".join(parts)


def _compile_and_run(driver_src: str, *, sanitize: bool, timeout_s: float):
    """Returns (stage, CompletedProcess|None, comp_stderr). stage in {"compile_error","timeout","ran"}."""
    with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
        f.write(driver_src)
        src = f.name
    exe = src + ".out"
    try:
        cmd = ["gcc", "-O0", "-fsigned-char", "-Wreturn-type"] + (_SANITIZE_FLAGS if sanitize else []) + ["-o", exe, src]
        comp = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if comp.returncode != 0:
            return "compile_error", comp, comp.stderr
        try:
            run = subprocess.run([exe], capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return "timeout", None, comp.stderr
        return "ran", run, comp.stderr
    finally:
        for p in (src, exe):
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            import shutil
            shutil.rmtree(exe + ".dSYM", ignore_errors=True)
        except Exception:
            pass


def replay_counterexample(
    task: Task, candidate_src: str, ce: dict | None, *, timeout_s: float = 10
) -> ReplayResult:
    """Replay a counterexample (as produced by `verify()`) on compiled code."""
    if not ce or ce.get("error") or not (ce.get("inputs") or ce.get("buffers")):
        return ReplayResult("not_applicable", "no counterexample values")

    is_ub = ce.get("kind") == "ub"
    driver = build_replay_driver(task, candidate_src, ce)
    stage, proc, comp_err = _compile_and_run(driver, sanitize=is_ub, timeout_s=timeout_s)

    if stage == "compile_error":
        return ReplayResult("driver_error", f"driver failed to compile: {proc.stderr[:300]}")
    if stage == "timeout":
        # A hang on the counterexample is itself a divergence only for UB cases.
        return ReplayResult("replayed" if is_ub else "driver_error", "driver timed out")

    out, err = proc.stdout, proc.stderr
    if is_ub:
        tripped = proc.returncode != 0 and ("runtime error" in err or "AddressSanitizer" in err)
        if "ORIG_OK" not in out:
            return ReplayResult("driver_error", "original failed before candidate ran: " + err[:200])
        if tripped:
            return ReplayResult("replayed", err.strip().splitlines()[0][:200] if err.strip() else "sanitizer")
        # UB may still surface as a visible difference
        if "DIFFERS" in out:
            return ReplayResult("replayed", "outputs differ")
        return ReplayResult("not_reproduced", "candidate ran clean under ASan/UBSan")

    if "DIFFERS" in out:
        return ReplayResult("replayed", "outputs differ")
    if "SAME" in out:
        # Falling off the end of a non-void candidate function is C UB; at -O0
        # the stale return register can coincidentally equal orig_func's result.
        if "non-void function does not return a value" in comp_err and "mut_func" in driver:
            return ReplayResult("replayed", "candidate UB: non-void function does not return a value")
        return ReplayResult("not_reproduced", "functions agree on counterexample input")
    return ReplayResult("driver_error", f"unexpected driver output (rc={proc.returncode}): {(out + err)[:200]}")
