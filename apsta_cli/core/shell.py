"""The only place apsta starts subprocesses.

Commands are always argv lists — never a shell string — so interface names,
SSIDs and other external values can't be interpreted by a shell. Modules call
``shell.run`` through the module attribute so tests can patch a single symbol.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional, Sequence

from .errors import SetupError

DEFAULT_TIMEOUT = 30


@dataclass
class Result:
    argv: Sequence[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def message(self) -> str:
        return (self.stderr or self.stdout).strip() or f"exit code {self.returncode}"

    def check(self, what: str) -> Result:
        """Raise :class:`SetupError` describing ``what`` failed, or return self."""
        if not self.ok:
            raise SetupError(f"{what} failed: {self.message()}")
        return self


def _search_path() -> str:
    # APSTA_PATH pins the search path exactly (integration tests use it to put a
    # fake toolchain first and nothing else). Otherwise add the sbin dirs that
    # root's PATH under sudo/pkexec/systemd sometimes lacks.
    pinned = os.environ.get("APSTA_PATH")
    if pinned:
        return pinned
    extra = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    return os.pathsep.join(filter(None, [os.environ.get("PATH"), extra]))


def which(name: str) -> Optional[str]:
    return shutil.which(name, path=_search_path())


def have(name: str) -> bool:
    return which(name) is not None


def run(
    argv: Sequence[str],
    *,
    input: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    daemonizes: bool = False,
) -> Result:
    """Run ``argv`` and capture its output.

    ``daemonizes=True`` is for programs that fork into the background: their
    output is captured through temp files instead of pipes, because a pipe
    stays open while the daemon child lives and would make us wait for it.
    """
    argv = [str(a) for a in argv]
    exe = argv[0] if os.path.isabs(argv[0]) else which(argv[0])
    if exe is None:
        return Result(argv, 127, "", f"{argv[0]}: command not found")
    env = {**os.environ, "LC_ALL": "C"}  # parse stable, untranslated tool output
    if daemonizes:
        return _run_daemonizing([exe, *argv[1:]], argv, timeout, env)
    try:
        proc = subprocess.run(
            [exe, *argv[1:]],
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        return Result(argv, 127, "", f"{argv[0]}: command not found")
    except subprocess.TimeoutExpired:
        return Result(argv, 124, "", f"{argv[0]}: timed out after {timeout:g}s")
    return Result(argv, proc.returncode, proc.stdout, proc.stderr)


def _run_daemonizing(cmd: Sequence[str], argv: Sequence[str], timeout: float, env: dict) -> Result:
    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        try:
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=timeout, env=env)
        except FileNotFoundError:
            return Result(argv, 127, "", f"{argv[0]}: command not found")
        except subprocess.TimeoutExpired:
            return Result(argv, 124, "", f"{argv[0]}: timed out after {timeout:g}s")
        out.seek(0)
        err.seek(0)
        return Result(argv, proc.returncode, out.read(), err.read())


def out(argv: Sequence[str], **kwargs) -> str:
    """Stripped stdout on success, '' on any failure."""
    result = run(argv, **kwargs)
    return result.stdout.strip() if result.ok else ""
