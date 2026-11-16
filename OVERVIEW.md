# Equivalence-Reward RL for C Code Optimization

**Goal:** train an LLM to optimize C code, where the reward comes from a formal proof of equivalence — not from hand-labeled examples or test suites.

## The problem

If an RL agent rewrites a C function to make it faster, how do you know it's still correct? You can't just run test cases — they might miss edge cases. You need a proof.

## The approach

Three layers:

### 1. Equivalence checker (Stage 0 — now)

Given two C functions (original + candidate rewrite), CBMC proves they're equivalent — same output for all inputs, no new bugs introduced. This is the reward signal. If CBMC says "equivalent," the agent gets a reward proportional to speedup. If CBMC says "not equivalent" or finds UB in the candidate, reward = 0.

Requirements: fast (sub-second median), never wrong (0 false proofs), works on real-world functions at scale.

### 2. Training loop (Stage 1+)

An LLM reads a C function, rewrites it, the checker scores the rewrite, and the model updates. The trick: the reward is binary on correctness (equivalent or not) but continuous on speed (how much faster). So the agent learns to find speedups that stay within the "provably correct" envelope.

### 3. The research bet

Formal verification as a reward has two properties missing from most RL-for-code work:
- (a) The reward is objective and unbounded — no human labels, no test coverage gaps.
- (b) The checker catches bugs the model might hide, making the system self-correcting.

The open question is whether there are enough functions with enough room for speedup to produce meaningful learning.

## Status

Stage 0 is the checkpoint: does the checker actually work at scale with the required throughput and reliability? Everything else depends on it.

**Current verdict: not go yet.** Five items remain before Stage 0 is complete:

1. Mutation study at scale (500 tasks, 0 false proofs, kill rate ≥95%, 100% counterexample replay)
2. Speed proxy validation (Spearman ≥0.8 on real equivalent-but-faster pairs)
3. x86-64 CI (GitHub Actions, golden suite + 200 tasks matching macOS results)
4. Dataset audit (exact counts, LOC/loop/pointer distributions)
5. Cleanup (report, golden test failures, stale decisions doc)