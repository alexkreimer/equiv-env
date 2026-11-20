"""Focused tests for Stage 2 UB narrowing and buffer-dimension length names."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from equiv.extract import _check_eligibility
from equiv.verify import check_original_ub, self_verify, verify
from equiv.testgen import run_tests


def _task(func_def: str, fname: str):
    rec = {
        "text": {
            "func_head": "",
            "func_def": func_def,
            "fname": fname,
            "path": "user/repo/ub_stage2.c",
            "real_error": "",
            "angha_deps": "",
        }
    }
    elig = _check_eligibility(rec, "train")
    assert elig.eligible, f"{fname} rejected: {elig.reason_code}"
    return elig.task


def test_stage2_non_negative_scalar_square_digits():
    """Real ExeBench Project Euler #92 function requires n >= 1 (negative n indexes pow2[-1])."""
    src = (
        "int square_digits(int n) {\n"
        "  const int pow2[] = {0, 1, 4, 9, 16, 25, 36, 49, 64, 81, 100};\n"
        "  int sum = 0;\n"
        "  while (n) { sum += pow2[n % 10]; n /= 10; }\n"
        "  return sum;\n"
        "}"
    )
    task = _task(src, "square_digits")
    ub_free, ranges = check_original_ub(task, timeout_s=30, maxn=8)
    assert ub_free, "square_digits should pass via Stage 2 narrowing"
    assert ranges and ranges.get("n") == [1, 128]
    task.input_ranges = ranges

    res = self_verify(task, timeout_s=30, maxn=8)
    assert res.status == "proven"

    t_res = run_tests(task, src, maxn=8, n_random=20)
    assert t_res.passed


def test_stage2_paired_length_off_by_one_strip_blank():
    """Real ExeBench strip_blank indexes buffer[len], requiring len in [1, MAXN - 1]."""
    src = (
        "void strip_blank(char *buffer, unsigned char len) {\n"
        "  for (unsigned char i = len; i > 0; i--) {\n"
        "    if (buffer[i] != ' ') break;\n"
        "    buffer[i] = 0;\n"
        "  }\n"
        "}"
    )
    task = _task(src, "strip_blank")
    ub_free, ranges = check_original_ub(task, timeout_s=30, maxn=8)
    assert ub_free, "strip_blank should pass via Stage 2 paired-length narrowing"
    assert ranges and ranges.get("len") == [1, 7]
    task.input_ranges = ranges

    res = self_verify(task, timeout_s=30, maxn=8)
    assert res.status == "proven"

    # Verify a wrong rewrite is still caught under Stage 2 ranges
    broken = (
        "void strip_blank(char *buffer, unsigned char len) {\n"
        "  buffer[len] = 'x';\n"
        "}"
    )
    bad = verify(task, broken, timeout_s=30, maxn=8)
    assert bad.status == "not_equivalent"


def test_width_paired_and_stage2_underflow_blur_sums():
    """Real ExeBench blur_gray_calc_sums pairs `width` and avoids width==0 underflow via Stage 2."""
    src = (
        "void blur_gray_calc_sums(uint8_t *src_img, uint8_t border_color,\n"
        "                         uint16_t *sum_tbl, unsigned int width) {\n"
        "  uint8_t *pix = src_img;\n"
        "  uint16_t a = border_color, b = *pix++;\n"
        "  for (unsigned x = 0; x < width - 1; x++) {\n"
        "    uint16_t c = *pix++;\n"
        "    sum_tbl[x] = a + b + c;\n"
        "    a = b; b = c;\n"
        "  }\n"
        "}"
    )
    task = _task(src, "blur_gray_calc_sums")
    assert task.pointer_rules == {"src_img": "paired", "sum_tbl": "paired"}

    ub_free, ranges = check_original_ub(task, timeout_s=30, maxn=8)
    assert ub_free, "blur_gray_calc_sums should pass with width paired + Stage 2 [1, 7]"
    assert ranges and ranges.get("width") == [1, 7]
    task.input_ranges = ranges

    res = self_verify(task, timeout_s=30, maxn=8)
    assert res.status == "proven"


def test_unconditional_ub_still_rejected():
    """Functions with unconditional UB (e.g. OOB access at index MAXN+2) still fail UB check."""
    src = "int bad_oob(int *a, int n) { return a[10]; }"
    task = _task(src, "bad_oob")
    ub_free, ranges = check_original_ub(task, timeout_s=30, maxn=8)
    assert not ub_free
    assert ranges is None
