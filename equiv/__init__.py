"""equiv — Equivalence Checker Environment for C."""

from .extract import Task, Signature, Eligibility, extract_from_exebench
from .harness import generate_harness, generate_self_harness, HarnessResult
from .verify import verify, self_verify, VerifyResult, verify_batch
from .testgen import run_tests, TestResult
from .cache import EvalCache
from .reward import evaluate, reward, evaluate_batch, EvalResult, init as reward_init

__all__ = [
    "Task", "Signature", "Eligibility", "extract_from_exebench",
    "generate_harness", "generate_self_harness", "HarnessResult",
    "verify", "self_verify", "VerifyResult", "verify_batch",
    "run_tests", "TestResult",
    "EvalCache",
    "evaluate", "reward", "evaluate_batch", "EvalResult", "reward_init",
]