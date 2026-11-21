# Stage 0 Report: Equivalence Checker Environment for C

**Date:** 2026-11-21
**Repo:** https://github.com/alexkreimer/equiv-env
**Platforms verified:** macOS arm64 (Apple Silicon) + x86-64 Linux (`ubuntu-26.04`, GitHub Actions run [44876998465](https://github.com/alexkreimer/equiv-env/actions/runs/44876998465)), CBMC 6.11.0

---

## 1. Funnel table (69,206 real-split functions processed)

| Stage | Count | % of processed |
|-------|------:|---------------:|
| Functions processed (`train_real_compilable` + `test_real` + `valid_real`) | 69,206 | 100.0% |
| Passed extraction & source/cap filters | 11,132 | 16.1% |
| Passed compile check (`gcc`/`clang`, `-O0`/`-O3`) | 10,623 | 15.4% |
| Passed UB-in-original check (Stage 1 + Stage 2 narrowing) | 8,012 | 11.6% |
| **Accepted (passed self-equivalence + unwind search)** | **6,968** | **10.07%** |

Rejection reasons (62,238 total):
- `unsupported_type` (float/double/struct/double-ptr): 19,536 (28.2%)
- `void_params` (no parameters): 17,226 (24.9%)
- `calls_external_function`: 11,078 (16.0%)
- `forbidden_construct` (inline asm, varargs, malloc, I/O, static locals): 5,376 (7.8%)
- `excluded_source` (`gcc/testsuite`, `tigress`, `obfuscated-programs`, etc.): 3,009 (4.3%)
- `ub_in_original`: 2,611 (3.8%)
- `self_equiv_fail`: 1,044 (1.5%)
- `not_compilable`: 895 (1.3%)
- `compile_fail`: 509 (0.7%)
- `cap_exceeded_file`: 444 (0.6%)
- `uses_global_variables`: 358 (0.5%)
- `libc_not_modeled`: 122 (0.2%)
- `no_signature`: 30 (0.04%)

---

## 2. Accepted task counts — exact (from `data/tasks_*.jsonl`)

| Split | Exact Tasks | Target | With Loops | Input-Dep Loops | Paired `(ptr, len)` | Median LOC (all / loops) |
|-------|------------:|-------:|-----------:|----------------:|--------------------:|-------------------------:|
| `tasks_train.jsonl` | **5,500** | ≥5,000 | 1,581 (28.7%) | 1,207 (21.9%) | 757 | 6 / 10 |
| `tasks_test.jsonl` (`test_real` + `valid_real`, deduped vs train) | **1,468** | ≥500 | 540 (36.8%) | 406 (27.7%) | 183 | 7 / 11 |

---

## 3. Self-check time distribution

| Split | Median | p90 | Target |
|-------|-------:|----:|--------|
| `tasks_train.jsonl` (5,500 tasks) | **0.29 s** | **0.65 s** | median < 20 s, p90 < 60 s (**PASS**) |
| `tasks_test.jsonl` (1,468 tasks) | **0.31 s** | **0.94 s** | median < 20 s, p90 < 60 s (**PASS**) |

---

## 4. Test suite & x86-64 CI verification (Review 2 C1)

- **x86-64 CI run:** [44876998465](https://github.com/alexkreimer/equiv-env/actions/runs/44876998465) (`ubuntu-26.04`, PR #16) — **SUCCESS**.
- **Full pytest suite:** **87/87 passed** on x86-64 Linux (golden set, char signedness agreement across gcc/clang/CBMC, libc models, void pointers, POSIX/Linux typedefs, Stage 2 UB narrowing, CE replay, speed proxy) + **5/5** new wired `evaluate()` pipeline tests (PR #15).
- **200-task CI fixture** (`ci/fixtures/tasks_200.jsonl`: 60 loop + 140 loop-free sampled from the new 5,500-task train build): **200/200 passed (100%)** on both macOS arm64 and x86-64 Linux.

---

## 5. Mutation study & counterexample replay (Review 3 item 6)

| Subset | Tasks | Mutants | Truly Different | Kill Rate | True False Proofs | CE Replay |
|--------|------:|--------:|----------------:|----------:|------------------:|-----------|
| Full train sample | 500 | 979 | 529 | **98.7%** | **0**\* | **698/726 (96.1%)** — 98.4% of runnable (11 `not_reproduced`, 17 `driver_error`) |
| Input-dependent loops only | 200 | 498 | 296 | **98.6%** | **0**\* | **385/403 (95.5%)** — 99.7% of runnable (1 `not_reproduced`, 17 `driver_error`) |

*\*All initial false-proof flags (`lba_to_msf`, `utf8_cnt`, `_abs`, `convert_to_lowercase_n`) were caused by `heavy_random_test()` sampling out-of-range inputs outside `task.input_ranges` / `(-2^30, 2^30)`. After aligning `heavy_random_test()` to the harness input domain (PR #17), re-verification on the flagged functions yielded **0 false proofs**.*

---

## 6. Speed proxy validation (x86-64 Linux, run [44876998465](https://github.com/alexkreimer/equiv-env/actions/runs/44876998465))

Measured on 19 real equivalent-but-faster source-level pairs (golden `EQ_FASTER` + loop/buffer pairs at `BENCH_N = 512`, separate translation units, interleaved in-process timing):

| Comparison | Spearman ρ | Target |
|------------|-----------:|--------|
| `callgrind` vs `inproc_time` (n=19, x86-64) | **0.817** | ≥ 0.8 (**PASS**) |
| `inproc_time` repeatability (run 1 vs run 2, x86-64) | **0.982** | — |
| `static_asm` vs `inproc_time` (comparison row) | 0.422 | — |

Per Review 2 C3, every `SpeedResult` records the machine fingerprint and flags, refuses to run under emulation, and verifies a large-input (`BENCH_N = 512`) output + buffer checksum (`outputs_match`) to reject `n > MAXN` bound-overfitting exploits.

---

## 7. Go / no-go summary

| Criterion | Result | Status |
|-----------|--------|--------|
| ≥ 5,000 accepted train tasks | **5,500 exact** (1,581 with loops, 1,207 input-dependent) | **PASS** |
| ≥ 500 accepted test tasks | **1,468 exact** (540 with loops, 406 input-dependent) | **PASS** |
| Median self-check < 20 s, p90 < 60 s | **0.29 s / 0.65 s** (train), **0.31 s / 0.94 s** (test) | **PASS** |
| 0 false proofs, kill rate ≥ 95% | **0 true false proofs**, **98.7%** (full) / **98.6%** (loops) | **PASS** |
| Counterexample replay | **96.1%** overall / **98.4–99.7%** of runnable (driver errors separated) | **PASS** |
| Full test suite + 200-task fixture on x86-64 CI | **87/87 pytest + 200/200 fixture** ([run 44876998465](https://github.com/alexkreimer/equiv-env/actions/runs/44876998465)) | **PASS** |
| Speed proxy Spearman ≥ 0.8 | **0.817** (`callgrind` vs `inproc_time`, x86-64) | **PASS** |

**Verdict: GO (once PRs #15–#18 are merged).** Ready for the Stage 0.5 pilot (8 rewrites from a strong model on 100–200 loop-heavy tasks through `evaluate()` to measure real speedup headroom over `gcc -O3` and early hidden-bug rate).
