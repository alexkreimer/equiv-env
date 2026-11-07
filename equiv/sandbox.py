"""
sandbox.py — Safe execution of untrusted (model-generated) C code.

Uses subprocess isolation with resource limits. Every compile and execution
of candidate code must run:
- In a temporary directory
- With no network access
- With CPU-time, wall-time, memory, and output-size limits
- Never writing outside its temp dir.

For v1: subprocess with timeout + tmpdir. nsjail/bubblewrap deferred to Phase 1.

Usage:
    from equiv.sandbox import sandbox_run, sandbox_compile
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import resource
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SandboxResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    time_s: float = 0.0
    killed: bool = False
    details: str = ""


# Limits
DEFAULT_CPU_LIMIT_S = 30
DEFAULT_MEMORY_LIMIT_MB = 512
DEFAULT_OUTPUT_LIMIT_BYTES = 10 * 1024 * 1024  # 10 MB


class Sandbox:
    """Isolated execution environment for untrusted code."""

    def __init__(self, work_dir: str | None = None):
        self.work_dir = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="equiv_sandbox_"))
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def compile(
        self,
        source_code: str,
        output_name: str = "a.out",
        compiler: str = "gcc",
        flags: list[str] | None = None,
        timeout_s: float = DEFAULT_CPU_LIMIT_S,
    ) -> SandboxResult:
        """Compile C source in the sandbox."""
        src_path = self.work_dir / f"{output_name}.c"
        out_path = self.work_dir / output_name

        src_path.write_text(source_code)

        cmd = [compiler, "-O0", "-o", str(out_path), str(src_path)]
        if flags:
            cmd.extend(flags)

        return self._run(cmd, timeout_s=timeout_s)

    def run(
        self,
        binary_name: str = "a.out",
        timeout_s: float = DEFAULT_CPU_LIMIT_S,
        memory_mb: int = DEFAULT_MEMORY_LIMIT_MB,
        stdin_data: str | None = None,
    ) -> SandboxResult:
        """Run a compiled binary in the sandbox."""
        binary_path = self.work_dir / binary_name
        if not binary_path.exists():
            return SandboxResult(success=False, details=f"Binary not found: {binary_path}")

        cmd = [str(binary_path)]
        return self._run(cmd, timeout_s=timeout_s, memory_mb=memory_mb, stdin_data=stdin_data)

    def compile_and_run(
        self,
        source_code: str,
        output_name: str = "a.out",
        compiler: str = "gcc",
        compile_flags: list[str] | None = None,
        timeout_s: float = DEFAULT_CPU_LIMIT_S,
        memory_mb: int = DEFAULT_MEMORY_LIMIT_MB,
        stdin_data: str | None = None,
    ) -> SandboxResult:
        """Compile and run in one step."""
        comp_result = self.compile(source_code, output_name, compiler, compile_flags, timeout_s)
        if not comp_result.success:
            return SandboxResult(
                success=False,
                stderr=comp_result.stderr,
                details=f"Compilation failed: {comp_result.stderr[:200]}",
            )

        return self.run(output_name, timeout_s=timeout_s, memory_mb=memory_mb, stdin_data=stdin_data)

    def _run(
        self,
        cmd: list[str],
        timeout_s: float = DEFAULT_CPU_LIMIT_S,
        memory_mb: int = DEFAULT_MEMORY_LIMIT_MB,
        stdin_data: str | None = None,
    ) -> SandboxResult:
        """Run a command with resource limits."""
        start = time.monotonic()

        def preexec_fn():
            # Set memory limit (soft)
            try:
                mem_bytes = memory_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
            except (ValueError, resource.error):
                pass

            # New process group for clean kill
            try:
                os.setsid()
            except OSError:
                pass

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                cwd=str(self.work_dir),
                preexec_fn=preexec_fn,
                input=stdin_data,
            )
            elapsed = time.monotonic() - start
            return SandboxResult(
                success=proc.returncode == 0,
                stdout=proc.stdout[:DEFAULT_OUTPUT_LIMIT_BYTES],
                stderr=proc.stderr[:DEFAULT_OUTPUT_LIMIT_BYTES],
                exit_code=proc.returncode,
                time_s=elapsed,
            )

        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            return SandboxResult(
                success=False,
                killed=True,
                time_s=elapsed,
                details=f"Timeout after {timeout_s}s",
            )

    def cleanup(self):
        """Remove the sandbox directory."""
        try:
            shutil.rmtree(self.work_dir)
        except OSError:
            pass


# Convenience functions

def sandbox_compile(source_code: str, **kwargs) -> SandboxResult:
    """Compile C source in a temporary sandbox."""
    s = Sandbox()
    try:
        return s.compile(source_code, **kwargs)
    finally:
        s.cleanup()


def sandbox_run(binary_path: str, **kwargs) -> SandboxResult:
    """Run a binary in a temporary sandbox."""
    s = Sandbox()
    try:
        # Copy binary to sandbox
        import shutil
        dest = s.work_dir / "a.out"
        shutil.copy(binary_path, dest)
        return s.run("a.out", **kwargs)
    finally:
        s.cleanup()


def sandbox_compile_and_run(source_code: str, **kwargs) -> SandboxResult:
    """Compile and run C source in a temporary sandbox."""
    s = Sandbox()
    try:
        return s.compile_and_run(source_code, **kwargs)
    finally:
        s.cleanup()