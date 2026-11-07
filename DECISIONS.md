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