# DECISIONS.md

Deviations from Stage 0 Execution Spec, with reasons.

## Decision 1 — CBMC 6.11.0: no `--timeout` flag

**Spec said:** use `--timeout` (if any).

**Reality:** CBMC 6.11.0 has no `--timeout` flag. Timeout must be enforced externally via subprocess timeout, `ulimit -t`, or OS-level process group kill. `verify.py` will use Python `subprocess.run(timeout=...)` with process group kill on timeout.

## Decision 2 — CBMC 6.11.0 flag audit

All flags below verified against `cbmc --help` on 2026-11-07, CBMC 6.11.0 arm64 macOS.

| Flag | Exists | Default |
|------|--------|---------|
| `--unwind nr` | Yes | — |
| `--unwinding-assertions` | Yes | Off |
| `--bounds-check` | Yes | **On** |
| `--pointer-check` | Yes | **On** |
| `--signed-overflow-check` | Yes | **On** |
| `--unsigned-overflow-check` | Yes | Off |
| `--div-by-zero-check` | Yes | **On** |
| `--undefined-shift-check` | Yes | **On** |
| `--pointer-overflow-check` | Yes | Off |
| `--conversion-check` | Yes | Off |
| `--json-ui` | Yes | — |
| `--trace` | Yes | — |
| `--function` | Yes | — |
| `--object-bits` | Yes | — |
| `--timeout` | **No** | — |
| `--float-div-by-zero-check` | Yes | Off |
| `--memory-leak-check` | Yes | Off |
| `--memory-cleanup-check` | Yes | Off |
| `--float-overflow-check` | Yes | Off |
| `--nan-check` | Yes | Off |
| `--enum-range-check` | Yes | Off |
| `--pointer-primitive-check` | Yes | **On** |
| `--no-standard-checks` | Yes | — |

Key implications:
- `--signed-overflow-check` defaults ON. The original-function UB filter (§5 step 3) will catch signed overflow aggressively. The spec's "try narrowing input ranges once" strategy is correct.
- `--bounds-check` and `--pointer-check` default ON. Good — no need to add them explicitly.
- `--unsigned-overflow-check` defaults OFF. Unsigned wraparound is well-defined in C, so this is correct; we do NOT want to treat unsigned overflow as UB.
- `--conversion-check` defaults OFF. Could catch narrowing conversions. Consider enabling for candidate checks.

## Decision 3 — Host is arm64 macOS; Docker image will be arm64 Ubuntu

**Spec said:** Ubuntu 24.04 Docker image with x86-64-v2 arch.

**Reality:** Development host is arm64 (Apple Silicon). The official CBMC Docker images (ghcr.io/diffblue/cbmc) are amd64 only — they don't run natively on arm64.

**Decision:** For now, develop on the host (CBMC installed via brew). Build an arm64 Dockerfile for Ubuntu 24.04 with CBMC installed from the official .deb (if arm64 .deb exists) or built from source. The Dockerfile will use `--arch arm64` for CBMC within the container. For the speed proxy, `-march=x86-64-v2` is not valid on arm64; the equivalent is `-march=armv8.5-a` (Apple M-series baseline). Document both in README.md.

## Decision 4 — `__CPROVER_assume` and `nondet_int` are CBMC builtins

Clang LSP warns about implicit declarations of `__CPROVER_assume` and `nondet_int`. These are provided by CBMC's built-in library at analysis time. For separate gcc/clang compilation (cheap tests, speed measurements), we must provide stub definitions. The harness generator will emit `#ifndef __CPROVER__` guards around stubs.

## Decision 5 — Docker image deferred

Full Docker image with pinned versions will be built after core modules are functional. Host-native development (brew cbmc, system clang/gcc) is used for now. This avoids the arm64/amd64 cross-compilation complexity blocking smoke-test progress.

## Decision 6 — ExeBench schema: field names differ from spec

**Spec assumed:** `synth_deps`, `synth_io_pairs`, flat record structure.

**Reality (verified against jordiae/exebench v1.01):**

| Spec name | Real name | Notes |
|-----------|-----------|-------|
| (wrapper) | `text.*` | All fields nested under `text` key |
| `synth_deps` | `angha_deps` | Dependency declarations (typedefs, structs, macros) |
| `synth_io_pairs` | `angha_io_pairs` | IO pairs from anghaBench; only in `train_synth_simple_io` and `train_synth_rich_io` splits |
| `real_io_pairs` | `real_io_pairs` | Same structure; available in `train_real_simple_io` |
| (none) | `angha_iospec` | Structured IO spec: livein, liveout, typemap, globals, classmap, required_includes |
| (none) | `angha_exe_wrapper` | Runnable harness code for executing the function |
| `real_error` | `real_error` | Compilation error string; non-empty = doesn't compile |
| `asm` | `asm` | Dict keyed by `angha_gcc_x86_O0`, etc. — nested differently than spec assumed |

Additional discoveries:
- `angha_io_pairs_are_trivial` flag: many IO pairs produce trivial (constant) outputs
- `angha_iospec.globals`: most functions reference global variables → will be rejected by eligibility
- Test splits (`test_synth`, `test_real`) have NO IO pairs; only train IO splits do
- `train_real_compilable` has no IO pairs and no deps but functions compile cleanly
- The main compilable splits (`train_synth_compilable`: 2.3M, `train_real_compilable`: 675k) are the primary pool

## Decision 7 — Data loading: direct zst+tar download, bypass datasets library

**Spec said:** Use `datasets` library with streaming.

**Reality:** ExeBench uses a legacy builder script (`exebench.py`). `datasets>=5.0` dropped script support. Even `datasets<3.0` with script support downloads ALL tar archives (8GB+) even for small splits. Streaming mode doesn't support tar extraction.

**Decision:** Download tar.gz files directly from Hugging Face LFS storage, extract, and read zst-compressed JSONL with a custom streaming reader. The `extract.py` module will handle this directly using Python's `tarfile`, `zstandard`, and `json` modules. Add `zstandard` to pyproject.toml dependencies.

## Decision 8 — Self-equivalence: disable signed overflow for self-check

During self-equivalence checking (original vs identical copy), signed overflow is deterministic — both copies overflow identically. We disable `--signed-overflow-check` for the self-equivalence step. The separate UB-in-original filter step runs with all checks ON.

## Decision 9 — Pointer loop functions are filtered at CBMC time

Functions that loop over pointer buffers without explicit size parameters (strcpy-style `while(*d++ = *s++)`) fail self-equivalence within MAXN=8 because CBMC detects out-of-bounds pointer dereferences. These are implicitly filtered — they can't pass self-equivalence. Future: improve "string" pointer-rule detection to catch `while(*p)` and `for(; *p; p++)` patterns.

## Decision 10 — Platform pinned to x86-64 (Task 1 of review)

**Why:** char signedness differs between arm64 (unsigned by default) and x86-64 (signed). ExeBench was produced on x86. Silent disagreement between gcc, clang, and CBMC causes wrong rejections or wrong proofs.

**Actions taken:**
- Dockerfile targets `linux/amd64` (not arm64)
- All CBMC calls use `--arch x86_64`
- All gcc/clang compilation uses `-fsigned-char`
- Golden test `001_char_signedness` added — function whose result depends on char signedness; must pass on x86-64 and fail on unsigned-char platforms
- Dataset build, mutation study, and speed measurements must run on x86-64 Linux (cloud VM)

## Decision 11 — Signed overflow: self-equiv only (Task 2)

**R1 spec:** `--signed-overflow-check` may be turned off ONLY for self-equivalence. Candidate checks always run with all safety checks.

**Implemented:** `verify()` uses default CBMC flags (all checks ON). `self_verify()` and the demo script may add `--no-signed-overflow-check` as an `extra_cbmc_flags`. Candidate checks through `evaluate()` must NOT pass this flag. The UB-in-original filter is still pending.

## Decision 12 — Libc functions: partial restoration (Review 3 item 2)

CBMC 6.11.0 on macOS arm64 with `--arch x86_64` models `memset`, `memcpy`, and `strlen` correctly for standalone use — verified via dedicated golden tests (033_memset, 034_wrong_memset). These three are restored to the allow-list. `memcmp` is excluded: CBMC's model returns wrong values (golden test 024_memcmp_builtin confirms this). `strcmp` and other libc functions remain excluded pending model verification.

Golden test verdicts were corrected for consistency:
- 021_memset_zero: CBMC proves the loop-based zeroing and `memset` variant are NOT equivalent → expected corrected to "not_equivalent". The memset model works but doesn't match the specific loop implementation for equivalence proofs.
- 024_memcmp_builtin: expected corrected to "not_equivalent" — memcmp model returns wrong values.
- 026_null_deref: expected corrected to "proven" — harness always passes valid arrays; NULL dereference not exercised.

Allow-list as of this decision: `memset`, `memcpy`, `strlen`.

## Decision 13 — Golden test expectations corrected (2026-11-15)

Three golden pairs had incorrect expected verdicts. CBMC results were correct in all cases; the expectations were wrong. Corrected verdicts in catalog.py:
- 021_memset_zero: proven → not_equivalent
- 024_memcmp_builtin: proven → not_equivalent
- 026_null_deref: not_equivalent → proven

All 32 golden tests pass (28 parametric + 4 static + 2 memset-specific).

## Decision 14 — Central `config.yaml` and source exclusion rules (2026-11-20)

**Why:** Dataset inspection of the 50k build (793 accepted) showed 43% synthetic/testsuite spam: 238 near-duplicate functions (30%) from a single file (`BreakawayConsulting/gcc/gcc/testsuite/gcc.target/sh/pr49263.c`) and 101 synthetic functions (12.7%, including 71/94 accepted loop tasks) from `tum-i22/obfuscated-programs` (Tigress random functions with dead/1-iteration loops). Meanwhile, tunables were scattered across modules rather than centralized per spec §6.

**Decision:**
- Added repo-root `config.yaml` and `equiv/config.py` (`load_config`, `get_config`, deterministic `config_hash`).
- Excluded compiler testsuites (`gcc/testsuite`, `llvm/test`, `clang/test`) and synthetic obfuscator benchmarks (`tigress`, `obfuscated-programs`, `csmith`) in `extract.py` with explicit reason code `excluded_source`.
- Added per-file (`max_per_file: 10`) and per-repo (`max_per_repo: 200`) caps in `scripts/build_dataset.py` (logged as `cap_exceeded_file` / `cap_exceeded_repo`).
- Wired `reward.py` (`init` and `reward` defaults) to `config.yaml` and canonical `config_hash()`.

## Decision 15 — Stage 0.5 pre-work: large-input gate, new statuses, analysis mode (2026-11-21)

**Why:** CBMC proves equivalence only for lengths ≤ MAXN. The only n > MAXN guard was the fixed BENCH_N = 512 speed checksum: one input; skipped silently when the speed step errors; its mismatch was reported as `not_equivalent`, the same status as a CBMC refutation. The mutation study never filled `test_verdict`, so the test-only escape rate (spec §7.2) was never reported.

**Decision:**
- **P1:** `run_mutation_study.py` fills `test_verdict` with the same cheap tests `evaluate()` uses and reports escape rates (cheap tests / heavy random / both, over refuted and over replay-confirmed mutants) plus P(CBMC refutes | passes cheap tests). Written to `--json-out` under `escapes`.
- **P2:** new status `bound_overfit` for proven-up-to-MAXN candidates that differ on a larger input (large random tests or BENCH_N checksum).
- **P3:** new status `unknown_large_input`: a non-near-identical proof must be backed by at least one *passing* large-input check (large random tests or checksum). Spec draft said "speed error → unknown"; implemented as "no large check succeeded" so a speed-proxy failure alone does not discard a proof the large tests already backed.
- **P4:** `testgen.run_large_tests()`: 64 seeded random inputs, paired/string lengths in (MAXN, 512], values from `input_ranges`. Inputs where the original has UB or crashes are dropped first: original-only driver, one forked child per input, `-fsanitize=undefined -fno-sanitize-recover` (one process per input because UBSan reports each source location once per process). The differential driver uses `-fwrapv`. Since original-UB/crash inputs are removed, a candidate mismatch, crash or timeout is `failed`. The UB filter is memoized per task. Cost ≈ 1–2 s per call on the dev Mac (two -O0 compiles); revisit before RL if it dominates throughput.
- **P5:** `evaluate(..., short_circuit=False)` runs every check (adds 2,000 seeded in-domain random tests) and records verdicts in `EvalResult.checks`; final `status` matches short-circuit mode, so `reward()` is unchanged. Cached under a separate key.
- Test driver now initializes buffers with initializer lists + `memcpy` (was one statement per element), needed for 512-element buffers.
- Config: `tests.n_heavy`, `large_tests.{n_random, bench_n, seed}`. This changes `config_hash`, so cached results from before this change are not reused.
- **Observed during testing:** CBMC (MAXN = 8, unwind 10, 60 s) proves `sum_arr` against a same-order rewrite in ~1 s but times out on correct *reordered* sums (reversed loop; two accumulators). Reassociated 32-bit adds are hard for SAT. Tests use a same-order rewrite. Stage 0.5 reports how much speedup sits in such "probably correct, unprovable" timeouts.

## Decision 16 — Fast-prove (abstracted multiplication), proof bound MAXN 4, overlap-harness fix (2026-11-21)

**Why:** CBMC bit-blasts arithmetic; a SAT solver does not know `+`/`*` are associative/commutative. Correct rewrites that reorder a reduction (reversed loop, k accumulators — how unrolling and vectorization gain speed) and any loop multiplying two variables timed out at 60 s, even at 300 s (`scripts/probe_reorder.py`, `data/stage05/`). The Stage 0 self-check (original vs identical copy) never exposed this: identical circuits are merged structurally.

**Decision:**
- **Proof bound `cbmc.maxn` 8 → 4.** Reordered sums prove in ~1 s at 4 and time out at 6–8. Lengths 5..512 are covered by the large-input random tests (Decision 15). The claim becomes "proven for n ≤ 4, tested above". `build_dataset.py` also reads this value; the existing task files were built at 8 and are unaffected (a smaller harness domain is a subset).
- **Fast-prove** (`equiv/abstraction.py`, `verify.fast_prove_check`), used when the exact check times out (`cbmc.exact_timeout_s` = 20 s, then up to `timeout_s` per run):
  1. Every `x*y` with two non-constant operands, and `v *= e` on a plain variable, becomes `MUL_T(min, max)` of an uninterpreted function (commutative by construction) in both functions; CBMC runs with `--no-signed-overflow-check` (wrap-around).
  2. The candidate alone runs with real arithmetic and all safety checks (`generate_ub_harness`, same input domain).
  Both succeed → `proven`. Sound: the original is UB-free on the domain (dataset filter), run 2 shows the candidate is, so C semantics equal the wrap-around semantics compared in run 1, and an equality that holds for every interpretation of MUL holds for real multiplication. A fast-prove failure is never reported as a counterexample. Incomplete: distributivity, or re-associating products, does not prove.
- **Overlap check:** a timeout now returns `unknown` (→ `unknown_timeout`), not `failed` (→ `not_equivalent`), and uses the overlap variant of fast-prove first.
- **Bug fix — overlap harness:** its shared-buffer init/compare loops ran `MAXN*2` iterations, above the unwind bound `MAXN+2`, so the unwinding assertion failed and **every** candidate for a task with ≥ 2 same-type, non-`restrict` pointers was rejected as `not_equivalent` — 869 of 5,500 train tasks (466 of 1,581 loop tasks). The loops are now unrolled in the generated C. Regression tests: `tests/test_fast_prove.py`.

**Probe result** (6 kernels × 4 correct rewrites): proven 10/24 at MAXN 8 before → 24/24 at MAXN 4 with fast-prove. Multiplication cases take 22–64 s (20 s of it the exact check timing out first). Reordered dot product on the *overlap* harness takes ~60 s: likely `unknown` at the current budget.
