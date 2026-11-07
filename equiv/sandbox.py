"""
sandbox.py — Safe execution of untrusted (model-generated) C code.

Three isolation backends, selected by env EQUIV_SANDBOX:
  "docker"   — run inside equiv-env Docker container (--network none, unprivileged)
  "rlimits"  — subprocess with resource limits (macOS/fallback)

Usage:
    from equiv.sandbox import Sandbox
    s = Sandbox(backend="docker")
    result = s.compile_and_run(source_code)
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


DEFAULT_CPU_LIMIT_S = 30
DEFAULT_MEMORY_LIMIT_MB = 512
DEFAULT_OUTPUT_LIMIT_BYTES = 10 * 1024 * 1024


class Sandbox:
    """Isolated execution environment for untrusted C code."""

    def __init__(self, backend: str | None = None, work_dir: str | None = None):
        self.backend = backend or os.environ.get("EQUIV_SANDBOX", "rlimits")
        self.work_dir = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="equiv_sandbox_"))
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def compile(self, source_code: str, output_name: str = "a.out",
                compiler: str = "gcc", flags: list[str] | None = None,
                timeout_s: float = DEFAULT_CPU_LIMIT_S) -> SandboxResult:
        src_path = self.work_dir / f"{output_name}.c"
        src_path.write_text(source_code)

        if self.backend == "docker":
            return self._docker_run(
                [compiler, "-O0", "-fsigned-char", "-o", f"/work/{output_name}",
                 f"/work/{output_name}.c"], timeout_s=timeout_s)
        else:
            out_path = self.work_dir / output_name
            cmd = [compiler, "-O0", "-fsigned-char", "-o", str(out_path), str(src_path)]
            if flags:
                cmd.extend(flags)
            return self._rlimit_run(cmd, timeout_s=timeout_s)

    def run(self, binary_name: str = "a.out",
            timeout_s: float = DEFAULT_CPU_LIMIT_S,
            memory_mb: int = DEFAULT_MEMORY_LIMIT_MB,
            stdin_data: str | None = None) -> SandboxResult:
        if self.backend == "docker":
            return self._docker_run([f"/work/{binary_name}"],
                                    timeout_s=timeout_s, stdin_data=stdin_data)
        else:
            binary_path = self.work_dir / binary_name
            if not binary_path.exists():
                return SandboxResult(success=False, details=f"Binary not found: {binary_path}")
            return self._rlimit_run([str(binary_path)], timeout_s=timeout_s,
                                    memory_mb=memory_mb, stdin_data=stdin_data)

    def compile_and_run(self, source_code: str, output_name: str = "a.out",
                        compiler: str = "gcc", compile_flags: list[str] | None = None,
                        timeout_s: float = DEFAULT_CPU_LIMIT_S,
                        memory_mb: int = DEFAULT_MEMORY_LIMIT_MB,
                        stdin_data: str | None = None) -> SandboxResult:
        comp_result = self.compile(source_code, output_name, compiler, compile_flags, timeout_s)
        if not comp_result.success:
            return SandboxResult(success=False, stderr=comp_result.stderr,
                                details=f"Compilation failed: {comp_result.stderr[:200]}")
        return self.run(output_name, timeout_s=timeout_s, memory_mb=memory_mb,
                        stdin_data=stdin_data)

    def _docker_run(self, cmd: list[str], timeout_s: float = DEFAULT_CPU_LIMIT_S,
                    stdin_data: str | None = None) -> SandboxResult:
        docker_cmd = [
            "docker", "run", "--rm",
            "--network", "none",
            "--memory", f"{DEFAULT_MEMORY_LIMIT_MB}m",
            "--cpus", "1",
            "--user", "1000:1000",
            "-v", f"{self.work_dir}:/work",
            "--workdir", "/work",
            "equiv-env",
        ] + cmd
        start = time.monotonic()
        try:
            proc = subprocess.run(docker_cmd, capture_output=True, text=True,
                                  timeout=timeout_s, input=stdin_data)
            elapsed = time.monotonic() - start
            return SandboxResult(
                success=proc.returncode == 0,
                stdout=proc.stdout[:DEFAULT_OUTPUT_LIMIT_BYTES],
                stderr=proc.stderr[:DEFAULT_OUTPUT_LIMIT_BYTES],
                exit_code=proc.returncode, time_s=elapsed)
        except subprocess.TimeoutExpired:
            return SandboxResult(success=False, killed=True,
                                details=f"Timeout after {timeout_s}s")
        except FileNotFoundError:
            return self._rlimit_run(cmd, timeout_s=timeout_s, stdin_data=stdin_data)

    def _rlimit_run(self, cmd: list[str], timeout_s: float = DEFAULT_CPU_LIMIT_S,
                    memory_mb: int = DEFAULT_MEMORY_LIMIT_MB,
                    stdin_data: str | None = None) -> SandboxResult:
        start = time.monotonic()
        def preexec():
            try:
                resource.setrlimit(resource.RLIMIT_AS,
                                   (memory_mb * 1024 * 1024, memory_mb * 1024 * 1024))
            except (ValueError, resource.error):
                pass
            try:
                os.setsid()
            except OSError:
                pass
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s,
                                  cwd=str(self.work_dir), preexec_fn=preexec, input=stdin_data)
            elapsed = time.monotonic() - start
            return SandboxResult(
                success=proc.returncode == 0,
                stdout=proc.stdout[:DEFAULT_OUTPUT_LIMIT_BYTES],
                stderr=proc.stderr[:DEFAULT_OUTPUT_LIMIT_BYTES],
                exit_code=proc.returncode, time_s=elapsed)
        except subprocess.TimeoutExpired:
            return SandboxResult(success=False, killed=True,
                                details=f"Timeout after {timeout_s}s")

    def cleanup(self):
        try:
            shutil.rmtree(self.work_dir)
        except OSError:
            pass


def sandbox_compile(source_code: str, **kwargs) -> SandboxResult:
    s = Sandbox()
    try:
        return s.compile(source_code, **kwargs)
    finally:
        s.cleanup()


def sandbox_run(binary_path: str, **kwargs) -> SandboxResult:
    s = Sandbox()
    try:
        dest = s.work_dir / "a.out"
        shutil.copy(binary_path, dest)
        return s.run("a.out", **kwargs)
    finally:
        s.cleanup()


def sandbox_compile_and_run(source_code: str, **kwargs) -> SandboxResult:
    s = Sandbox()
    try:
        return s.compile_and_run(source_code, **kwargs)
    finally:
        s.cleanup()