"""On timeout, verify's CBMC runner must kill the whole process group, not just CBMC.

CBMC with an external solver (--z3, --smt2) spawns the solver as a child. Killing
only CBMC left solvers running for hours (seen in the Stage 0.5 reorder probe).
"""

import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from equiv.verify import run_cbmc_process


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed child of an exited parent may linger as a zombie until reaped.
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return bool(stat.strip()) and not stat.strip().startswith("Z")


def test_timeout_kills_grandchildren(tmp_path):
    pidfile = tmp_path / "pid"
    cmd = ["sh", "-c", f"sleep 300 & echo $! > {pidfile}; wait"]
    with pytest.raises(subprocess.TimeoutExpired):
        run_cbmc_process(cmd, timeout_s=1)
    grandchild = int(pidfile.read_text())
    deadline = time.time() + 5
    while _alive(grandchild) and time.time() < deadline:
        time.sleep(0.1)
    assert not _alive(grandchild), "solver-like grandchild survived the timeout"


def test_normal_completion_returns_output():
    r = run_cbmc_process(["sh", "-c", "echo out; echo err >&2; exit 3"], timeout_s=10)
    assert (r.returncode, r.stdout, r.stderr) == (3, "out\n", "err\n")
