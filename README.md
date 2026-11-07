# equiv-env — Equivalence Checker Environment for C

Stage 0 of the Equivalence-Reward RL project. Builds everything needed for RL training except the model: a dataset of checkable C functions, a CBMC-based equivalence verifier, a speed proxy, and a reward API.

## Setup

### Prerequisites

- macOS arm64 (Apple Silicon) or Linux arm64
- CBMC 6.11.0+ (`brew install cbmc` on macOS)
- Python 3.11+
- gcc and clang

### Install

```bash
pip install -e .
```

### Smoke test

```bash
# Equivalent: should pass
cbmc --function harness tests/golden/000_smoke/test_equiv.c

# Not equivalent: should fail with counterexample
cbmc --function harness --trace tests/golden/000_smoke/test_neq.c
```

## Architecture

```
equiv/
  extract.py     — parse C functions, check eligibility via clang AST
  harness.py     — generate CBMC equivalence harness
  verify.py      — run CBMC, parse results
  testgen.py     — cheap random/IO test pre-filter
  speed.py       — speed proxy measurement (callgrind / llvm-mca)
  reward.py      — public API: evaluate() and reward()
  sandbox.py     — safe execution of untrusted code
  cache.py       — result cache
  mutate.py      — mutation operators for validation
scripts/
  build_dataset.py
  run_self_equiv.py
  run_mutation_study.py
  make_report.py
```

## Key decisions

See [DECISIONS.md](DECISIONS.md) for all deviations from the execution spec.