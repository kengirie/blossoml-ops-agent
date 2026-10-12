"""Where commands run. Phase 1 implements local execution only; an SSH
executor with the same interface can be added if the agent moves to another VM.

No shell is ever involved: argv lists are fixed by the tool code.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Iterator, Protocol, Sequence

_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0"}
MAX_CAPTURE = 256 * 1024


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class Executor(Protocol):
    def run(self, argv: Sequence[str], timeout: float = 30.0) -> CommandResult: ...

    def stream_lines(self, argv: Sequence[str], timeout: float = 120.0, max_lines: int = 2_000_000) -> Iterator[bytes]: ...


def _check_argv(argv: Sequence[str]) -> list[str]:
    argv = list(argv)
    if not argv or not all(isinstance(a, str) for a in argv) or not argv[0].startswith("/"):
        raise ValueError("argv must be a non-empty list of strings with an absolute program path")
    return argv


class LocalExecutor:
    def run(self, argv: Sequence[str], timeout: float = 30.0) -> CommandResult:
        p = subprocess.run(
            _check_argv(argv), shell=False, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=timeout, env=_ENV, check=False,
        )
        return CommandResult(
            p.returncode,
            p.stdout[:MAX_CAPTURE].decode("utf-8", "replace"),
            p.stderr[:MAX_CAPTURE].decode("utf-8", "replace"),
        )

    def stream_lines(self, argv: Sequence[str], timeout: float = 120.0, max_lines: int = 2_000_000) -> Iterator[bytes]:
        import time

        deadline = time.monotonic() + timeout
        p = subprocess.Popen(
            _check_argv(argv), shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=_ENV,
        )
        try:
            assert p.stdout is not None
            for i, line in enumerate(p.stdout):
                if i >= max_lines or time.monotonic() > deadline:
                    break
                yield line
        finally:
            p.kill()
            p.wait(timeout=5)
