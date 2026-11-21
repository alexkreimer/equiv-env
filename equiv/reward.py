"""
reward.py — Public API: evaluate() and reward().

Usage:
    from equiv.reward import evaluate, reward, EvalResult

    result = evaluate(task_id, candidate_src)
    r = reward(result)
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Literal

from .cache import EvalCache
from .config import config_hash as compute_config_hash, get_config, load_config
from .extract import Task, _strip_comments
from .verify import verify, VerifyResult, self_verify, _run_cbmc_raw, _has_safety_failures


EvalStatus = Literal[
    "proven",
    "not_equivalent",
    "unknown_timeout",
    "unknown_unwind",
    "unsupported",
    "compile_error",
    "checker_error",
    "test_fail",
]


@dataclass
class EvalResult:
    """Full evaluation result for a single candidate."""
    status: EvalStatus
    task_id: str
    speedup: float = 1.0
    check_time_s: float = 0.0
    test_result: dict | None = None
    verify_result: dict | None = None
    overlap_check: str = "not_applicable"  # "passed" | "failed" | "not_applicable" | "skipped_original_ub"
    near_identical: bool = False
    details: str = ""


# Global cache instance (lazy init)
_cache: EvalCache | None = None
_config_hash: str = ""
_active_config: dict | None = None


def init(config: dict | None = None, cache_path: str | None = None):
    """Initialize the reward module with config (defaults to config.yaml) and cache."""
    global _cache, _config_hash, _active_config
    cfg = load_config() if config is None else config
    _active_config = cfg
    resolved_cache = cache_path or cfg.get("cache", {}).get("path", "data/eval_cache.db")
    _cache = EvalCache(resolved_cache)
    _config_hash = compute_config_hash(cfg)


_C_KEYWORDS = {
    "auto", "break", "case", "char", "const", "continue", "default", "do",
    "double", "else", "enum", "extern", "float", "for", "goto", "if",
    "inline", "int", "long", "register", "restrict", "return", "short",
    "signed", "sizeof", "static", "struct", "switch", "typedef", "union",
    "unsigned", "void", "volatile", "while", "_Bool", "bool", "size_t",
    "ssize_t", "ptrdiff_t", "int8_t", "uint8_t", "int16_t", "uint16_t",
    "int32_t", "uint32_t", "int64_t", "uint64_t", "NULL", "true", "false",
}
_IDENT_RE = re.compile(r'\b[A-Za-z_]\w*\b')
_LOCAL_LABEL_RE = re.compile(r'\b(?:LBB\d+_\d+|\.L\w+|Ltmp\d+)\b')


def _canonicalize_source(src: str) -> str:
    """Strip comments, collapse whitespace, and alpha-rename non-keyword identifiers."""
    cleaned = _strip_comments(src)
    mapping: dict[str, str] = {}

    def _repl(m: re.Match) -> str:
        tok = m.group(0)
        if tok in _C_KEYWORDS:
            return tok
        if tok not in mapping:
            mapping[tok] = f"v{len(mapping)}"
        return mapping[tok]

    canon = _IDENT_RE.sub(_repl, cleaned)
    return re.sub(r'\s+', ' ', canon).strip()


def _normalized_o3_asm(task: Task, func_src: str) -> str | None:
    """Compile a single function at -O3 and return normalized assembly instructions."""
    from .harness import _extract_function_name, _filter_deps, _header_for_task
    from .speed import _external_function_src, default_arch

    fname = _extract_function_name(func_src) or task.func_name
    body = _external_function_src(task, func_src, fname, "fut__cmp")
    if body is None:
        return None

    deps = _filter_deps(task.deps_src) if task.deps_src else ""
    tu = "\n".join([_header_for_task(task), deps, body, ""])
    arch = default_arch()
    flags = ["-O3", "-fsigned-char", "-fno-builtin", "-w"] + ([f"-march={arch}"] if arch else [])

    with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
        f.write(tu)
        src_path = f.name
    try:
        r = subprocess.run(
            ["gcc", *flags, "-S", "-o", "-", src_path],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return None
        insns: list[str] = []
        for line in r.stdout.splitlines():
            # Strip trailing inline comments
            line = re.sub(r'(?://|;|#).*$', '', line).strip()
            if not line or line.startswith(".") or line.endswith(":"):
                continue
            line = _LOCAL_LABEL_RE.sub("LBL", line)
            line = re.sub(r'\s+', ' ', line)
            insns.append(line)
        return "\n".join(insns) if insns else None
    except (subprocess.TimeoutExpired, OSError):
        return None
    finally:
        try:
            os.unlink(src_path)
        except OSError:
            pass


def check_near_identical(task: Task, candidate_src: str) -> bool:
    """Return True if candidate is a copy or cosmetic rewrite of the original.

    R1 spec Step 8:
      1. Canonicalized source (comments/whitespace stripped, identifiers renamed) matches, OR
      2. Both compile at -O3 to identical normalized assembly instructions.
    """
    if _canonicalize_source(task.func_src) == _canonicalize_source(candidate_src):
        return True
    orig_asm = _normalized_o3_asm(task, task.func_src)
    cand_asm = _normalized_o3_asm(task, candidate_src)
    if orig_asm is not None and cand_asm is not None and orig_asm == cand_asm:
        return True
    return False


def _run_overlap_check(
    task: Task,
    candidate_src: str,
    *,
    maxn: int,
    unwind: int,
    timeout_s: float,
    extra_cbmc_flags: list[str] | None,
) -> str:
    """Run the overlapping-buffer CBMC check (R1 spec Step 3 item 5).

    Returns: "not_applicable" | "skipped_original_ub" | "passed" | "failed"
    """
    from .harness import generate_overlap_harness

    cand_harness = generate_overlap_harness(task, candidate_src, maxn=maxn, unwind=unwind)
    if cand_harness is None:
        return "not_applicable"

    def _run_src(src: str, flags: list[str] | None) -> subprocess.CompletedProcess | None:
        with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
            f.write(src)
            path = f.name
        try:
            return _run_cbmc_raw(path, unwind, timeout_s, extra_flags=flags)
        except subprocess.TimeoutExpired:
            return None
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    # Check whether the original itself has UB under overlap
    orig_harness = generate_overlap_harness(task, task.func_src, maxn=maxn, unwind=unwind)
    if orig_harness is not None:
        orig_res = _run_src(orig_harness.harness_src, None)
        if orig_res is not None and _has_safety_failures(orig_res.stdout):
            return "skipped_original_ub"

    cand_res = _run_src(cand_harness.harness_src, extra_cbmc_flags)
    if cand_res is not None and "VERIFICATION SUCCESSFUL" in cand_res.stdout:
        return "passed"
    return "failed"


def evaluate(
    task: Task,
    candidate_src: str,
    *,
    timeout_s: float = 60,
    maxn: int = 8,
    skip_tests: bool = False,
    skip_speed: bool = False,
    extra_cbmc_flags: list[str] | None = None,
    _allow_overflow_off: bool = False,  # internal: only for self-equiv
) -> EvalResult:
    """Evaluate a candidate against the original function.

    Pipeline: cache → cheap tests → CBMC verify → overlap check →
              near_identical check → speed proxy (+ large-input checksum).
    """
    # Enforce: candidate checks MUST run with signed-overflow-check ON.
    # --no-signed-overflow-check is only allowed for self-equivalence.
    if extra_cbmc_flags and "--no-signed-overflow-check" in extra_cbmc_flags:
        if not _allow_overflow_off:
            raise ValueError(
                "--no-signed-overflow-check is only allowed for self-equivalence "
                "checks. Candidate checks must run with all safety checks ON."
            )

    start_time = time.monotonic()

    # Check cache
    if _cache and _config_hash:
        cached = _cache.get(task.task_id, candidate_src, _config_hash)
        if cached:
            return EvalResult(**cached)

    # 1. Normalize candidate
    candidate_src = candidate_src.strip()
    eff_unwind = task.unwind or (maxn + 2)

    # 2. Cheap test pre-filter
    if not skip_tests:
        from .testgen import run_tests
        test_result = run_tests(task, candidate_src, maxn=maxn)
        if not test_result.passed:
            result = EvalResult(
                status="test_fail",
                task_id=task.task_id,
                check_time_s=test_result.time_s,
                test_result={"first_failure": test_result.first_failure,
                             "details": test_result.details},
                details=f"Failed cheap tests: {test_result.details[:200]}",
            )
            _cache_put(task.task_id, candidate_src, result)
            return result
    else:
        test_result = None

    # 3. CBMC main verify (using task.unwind)
    verify_result = verify(
        task, candidate_src, timeout_s=timeout_s, maxn=maxn,
        unwind=eff_unwind, extra_cbmc_flags=extra_cbmc_flags,
    )

    status_map = {
        "proven": "proven",
        "not_equivalent": "not_equivalent",
        "unknown_timeout": "unknown_timeout",
        "unknown_unwind": "unknown_unwind",
        "unsupported": "unsupported",
        "compile_error": "compile_error",
        "checker_error": "checker_error",
    }

    status = status_map.get(verify_result.status, "checker_error")
    details = verify_result.details
    overlap_check = "not_applicable"
    near_identical = False
    speedup = 1.0

    # 4. Overlapping-buffer check (R1 spec Step 3 item 5)
    if status == "proven":
        overlap_check = _run_overlap_check(
            task, candidate_src, maxn=maxn, unwind=eff_unwind,
            timeout_s=timeout_s, extra_cbmc_flags=extra_cbmc_flags,
        )
        if overlap_check == "failed":
            status = "not_equivalent"
            details = "Failed overlapping-buffer equivalence check"

    # 5. Near-identical check + speed proxy (+ large-input differential checksum)
    if status == "proven":
        near_identical = check_near_identical(task, candidate_src)
        if not near_identical and not skip_speed:
            from .speed import have_valgrind, measure_speedup

            cfg_proxy = (_active_config or get_config()).get("speed", {}).get("proxy", "inproc_time")
            proxy = "callgrind" if (cfg_proxy == "callgrind" and have_valgrind()) else "inproc_time"
            sr = measure_speedup(task, candidate_src, proxy=proxy)
            if sr.error is None:
                if not sr.outputs_match:
                    status = "not_equivalent"
                    details = "Large-input checksum mismatch (possible bound-overfitting for n > MAXN)"
                    speedup = 1.0
                else:
                    speedup = sr.speedup

    check_time = time.monotonic() - start_time

    result = EvalResult(
        status=status,
        task_id=task.task_id,
        speedup=speedup,
        check_time_s=check_time,
        test_result={"passed": test_result.passed, "n_tests": test_result.n_tests}
        if test_result else None,
        verify_result={
            "status": verify_result.status,
            "counterexample": verify_result.counterexample,
            "check_time_s": verify_result.check_time_s,
            "details": verify_result.details,
        },
        overlap_check=overlap_check,
        near_identical=near_identical,
        details=details,
    )

    _cache_put(task.task_id, candidate_src, result)
    return result


def reward(
    r: EvalResult,
    cap: float | None = None,
    min_gain: float | None = None,
    base: float | None = None,
) -> float:
    """Compute reward from evaluation result.

    [R1] Reward structure (defaults loaded from config.yaml):
        - Not proven: 0.0
        - near_identical (copy/cosmetic rewrite): 0.0
        - Proven but below min_gain: base (default 0)
        - Proven and faster: min(speedup, cap) - 1.0
    """
    rcfg = (_active_config or get_config()).get("reward", {})
    cap = rcfg.get("cap", 4.0) if cap is None else cap
    min_gain = rcfg.get("min_gain", 1.05) if min_gain is None else min_gain
    base = rcfg.get("base", 0.0) if base is None else base

    if r.status != "proven":
        return 0.0
    if r.near_identical:
        return 0.0
    if r.speedup < min_gain:
        return base
    return min(r.speedup, cap) - 1.0


def _cache_put(task_id: str, candidate_src: str, result: EvalResult):
    """Cache the evaluation result if cache is configured."""
    if _cache and _config_hash:
        try:
            _cache.put(task_id, candidate_src, _config_hash, {
                "status": result.status,
                "task_id": result.task_id,
                "speedup": result.speedup,
                "check_time_s": result.check_time_s,
                "test_result": result.test_result,
                "verify_result": result.verify_result,
                "overlap_check": result.overlap_check,
                "near_identical": result.near_identical,
                "details": result.details,
            })
        except Exception:
            pass  # Cache failures shouldn't block evaluation


def evaluate_batch(
    tasks_candidates: list[tuple[Task, str]],
    *,
    timeout_s: float = 60,
    maxn: int = 8,
    n_workers: int = 1,
    extra_cbmc_flags: list[str] | None = None,
) -> list[EvalResult]:
    """Evaluate multiple (task, candidate) pairs.

    Uses a process pool when n_workers > 1. Does not crash on individual failures.
    """
    if n_workers <= 1:
        results = []
        for task, candidate_src in tasks_candidates:
            try:
                r = evaluate(task, candidate_src, timeout_s=timeout_s,
                             maxn=maxn, extra_cbmc_flags=extra_cbmc_flags)
            except Exception as e:
                r = EvalResult(
                    status="checker_error", task_id=task.task_id,
                    details=str(e),
                )
            results.append(r)
        return results

    from concurrent.futures import ProcessPoolExecutor, as_completed
    results = [None] * len(tasks_candidates)

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        future_to_idx = {}
        for i, (task, cand) in enumerate(tasks_candidates):
            future = executor.submit(
                evaluate, task, cand,
                timeout_s=timeout_s, maxn=maxn,
                extra_cbmc_flags=extra_cbmc_flags,
            )
            future_to_idx[future] = i

        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                # We don't have the task_id here — use empty string
                results[idx] = EvalResult(
                    status="checker_error", task_id="",
                    details=str(e),
                )

    return results