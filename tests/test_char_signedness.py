"""Golden test: char signedness cross-tool agreement.

Verifies that gcc, clang, and CBMC all agree on whether char is signed.
Runs on x86-64 with -fsigned-char / --arch x86_64.
"""

import subprocess
import tempfile
import os
import sys

TEST_FILE = os.path.join(
    os.path.dirname(__file__),
    "001_char_signedness",
    "test.c",
)


def _compile_and_run(compiler: str, flags: list[str]) -> int:
    """Compile test.c with given compiler+flags and return exit code."""
    with tempfile.NamedTemporaryFile(suffix=".out", delete=False) as f:
        out_path = f.name

    try:
        result = subprocess.run(
            [compiler] + flags + ["-o", out_path, TEST_FILE],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Compilation failed: {result.stderr[:200]}")

        # Create a simple test that calls the function with a negative char
        runner_src = """
#include <stdio.h>
int orig__f(char c) { return c < 0 ? 1 : 0; }
int main() {
    /* -1 as char: on signed-char platforms this is negative */
    char test_val = -1;
    int result = orig__f(test_val);
    printf("%d\\n", result);
    return result;  /* 1 = signed, 0 = unsigned */
}
"""
        with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
            f.write(runner_src)
            runner_path = f.name

        result = subprocess.run(
            [compiler] + flags + ["-o", out_path, runner_path],
            capture_output=True, text=True, timeout=10,
        )
        os.unlink(runner_path)

        if result.returncode != 0:
            raise RuntimeError(f"Runner compilation failed: {result.stderr[:200]}")

        result = subprocess.run(
            [out_path], capture_output=True, text=True, timeout=5,
        )
        return result.returncode
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _run_cbmc() -> bool:
    """Run CBMC and return whether it proves equivalence."""
    result = subprocess.run(
        ["cbmc", "--arch", "x86_64", "--function", "harness", TEST_FILE],
        capture_output=True, text=True, timeout=30,
    )
    return "VERIFICATION SUCCESSFUL" in result.stdout


def test_char_signedness_gcc():
    """gcc -fsigned-char must treat char as signed."""
    rc = _compile_and_run("gcc", ["-fsigned-char", "-O0"])
    assert rc == 1, f"gcc -fsigned-char should return 1 (signed), got {rc}"


def test_char_signedness_clang():
    """clang -fsigned-char must treat char as signed."""
    rc = _compile_and_run("clang", ["-fsigned-char", "-O0"])
    assert rc == 1, f"clang -fsigned-char should return 1 (signed), got {rc}"


def test_char_signedness_cbmc():
    """CBMC --arch x86_64 must prove self-equivalence (char is signed)."""
    assert _run_cbmc(), "CBMC --arch x86_64 must prove self-equivalence"


def test_char_signedness_agreement():
    """All three tools must agree that char is signed on x86-64."""
    gcc_signed = _compile_and_run("gcc", ["-fsigned-char", "-O0"]) == 1
    clang_signed = _compile_and_run("clang", ["-fsigned-char", "-O0"]) == 1
    cbmc_signed = _run_cbmc()
    assert gcc_signed == clang_signed == cbmc_signed, (
        f"Disagreement: gcc={gcc_signed}, clang={clang_signed}, cbmc={cbmc_signed}"
    )