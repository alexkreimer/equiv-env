# Equivalence-Reward RL for C Code Optimization

**Goal:** train an LLM to optimize C code, where the reward comes from a formal proof of equivalence — not from hand-labeled examples or test suites.

## The problem

If an RL agent rewrites a C function to make it faster, how do you know it's still correct? You can't just run test cases — they might miss edge cases. You need a proof.

## The approach

Three layers:

### 1. Equivalence checker (Stage 0 — complete)

Given two C functions (original + candidate rewrite), CBMC proves they're equivalent — same output for all inputs, no new UB introduced, including overlapping-buffer checks and a `BENCH_N = 512` large-input checksum. If proven equivalent and not `near_identical`, the agent gets a reward proportional to speedup (`min(speedup, cap) - 1`). Otherwise reward = 0.

### 2. Headroom & pairs pilot (Stage 0.5 — next)

Sample 8–16 rewrites from a strong LLM on 100–200 loop-heavy accepted tasks through `evaluate()` to measure real speedup headroom over `gcc -O3` and the initial hidden-bug rate (candidates passing cheap tests that CBMC refutes).

### 3. Training loop (Stage 1+)

GRPO on a ~7B open code model comparing test-only reward vs. formal equivalence-proof reward vs. hybrid, measuring hidden-bug rate, verified speedup, and counterexample bug taxonomy.

## Status

**Stage 0 verdict: GO** (all criteria met; see [STAGE0_REPORT.md](STAGE0_REPORT.md)):
- **Dataset:** 5,500 train tasks (1,581 with loops, 1,207 input-dependent) + 1,468 test tasks (540 with loops, 406 input-dependent); self-check median 0.29 s / p90 0.65 s.
- **Soundness:** 98.7% mutant kill rate (98.6% on loop-only), 0 true false proofs, 96.1% overall (98.4–99.7% of runnable) counterexample replay.
- **Speed proxy:** `callgrind` vs `inproc_time` Spearman = **0.817** on x86-64 Linux (`inproc_time` repeatability 0.982).
- **x86-64 CI:** 87/87 pytest + 200/200 refreshed fixture tasks green on `ubuntu-26.04`.
