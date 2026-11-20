"""Tests for build_dataset: real-only archive filtering, compile_check with
filtered deps, and parallel worker pool execution."""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from equiv.extract import _check_eligibility
from scripts.build_dataset import compile_check, find_zst_files, _verify_candidate_task


def test_find_zst_files_skips_synth_by_default(tmp_path: Path):
    """find_zst_files excludes train_synth_simple_io and synth_test unless include_synth=True."""
    for sub in ("train_real_compilable", "real_test", "valid_real", "train_synth_simple_io", "synth_test"):
        d = tmp_path / sub
        d.mkdir()
        (d / "part0.jsonl.zst").write_bytes(b"")

    real_only = find_zst_files(str(tmp_path), include_synth=False)
    assert len(real_only) == 3
    assert all("synth" not in p for p in real_only)

    all_files = find_zst_files(str(tmp_path), include_synth=True)
    assert len(all_files) == 5


def test_compile_check_strips_fake_anghabench_typedefs():
    """compile_check uses _header_for_task + _filter_deps so fake typedefs don't break gcc/clang."""
    rec = {
        "text": {
            "func_head": "",
            "func_def": "__u64 add_one(__u64 x) { return x + 1; }",
            "fname": "add_one",
            "path": "user/repo/a.c",
            "real_error": "",
            "angha_deps": "typedef char* __u64 ;\n",
        }
    }
    elig = _check_eligibility(rec, "train")
    assert elig.eligible
    assert compile_check(elig.task), "compile_check failed on function with fake AnghaBench __u64 typedef"

    status, task, _, _, _, elapsed = _verify_candidate_task(
        elig.task, "h1", "user/repo/a.c", "user/repo", 15.0, 30.0, 8, [10, 34, 66]
    )
    assert status == "accepted"
    assert elapsed > 0
