"""Shared test helpers: isolated paths, a scriptable fake shell, fixtures."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Union
from unittest import mock

from apsta_cli.core import paths, shell

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def isolate_paths(testcase, root: Optional[Path] = None) -> Path:
    """Point every apsta path into a fresh temp dir for the duration of the test."""
    if root is None:
        tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
    etc, run = root / "etc", root / "run"
    values = {
        "CONFIG_DIR": etc,
        "CONFIG_PATH": etc / "config.json",
        "SECRETS_PATH": etc / "secrets.json",
        "RUN_DIR": run,
        "STATE_PATH": run / "state.json",
        "LOCK_PATH": run / "lock",
        "RESUME_MARKER": run / "resume-pending",
        "HOSTAPD_CONF": run / "hostapd.conf",
        "HOSTAPD_CTRL_DIR": run / "hostapd",
        "HOSTAPD_PID": run / "hostapd.pid",
        "DNSMASQ_CONF": run / "dnsmasq.conf",
        "DNSMASQ_PID": run / "dnsmasq.pid",
        "DNSMASQ_LEASES": run / "dnsmasq.leases",
        "NM_RUNTIME_KEYFILE_DIR": run / "NetworkManager",
        "NM_RUNTIME_CONF_DIR": run / "NetworkManager-conf.d",
        "LOG_PATH": root / "apsta.log",
        "SYSFS_NET": root / "sys" / "class" / "net",
        "IP_FORWARD": root / "ip_forward",
    }
    values["SYSFS_NET"].mkdir(parents=True, exist_ok=True)
    values["IP_FORWARD"].write_text("0\n")
    for name, value in values.items():
        patcher = mock.patch.object(paths, name, value)
        patcher.start()
        testcase.addCleanup(patcher.stop)
    return root


Response = Union[shell.Result, Callable[[List[str]], shell.Result]]


class FakeShell:
    """Replaces ``shell.run``: answers by argv prefix and records every call."""

    def __init__(self, tools: Sequence[str] = ()):
        self.calls: List[List[str]] = []
        self.inputs: List[Optional[str]] = []
        self._rules: List[tuple] = []
        self.tools = set(tools)

    def on(self, *prefix: str, stdout: str = "", rc: int = 0, stderr: str = "", fn: Optional[Callable] = None):
        self._rules.append((list(prefix), fn, rc, stdout, stderr))
        return self

    def __call__(self, argv, input=None, timeout=None, daemonizes=False):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        self.inputs.append(input)
        for prefix, fn, rc, stdout, stderr in reversed(self._rules):
            if argv[: len(prefix)] == prefix:
                if fn is not None:
                    return fn(argv)
                return shell.Result(argv, rc, stdout, stderr)
        return shell.Result(argv, 0, "", "")

    def install(self, testcase) -> FakeShell:
        for target, value in (
            ("apsta_cli.core.shell.run", self),
            ("apsta_cli.core.shell.which", lambda name: f"/usr/bin/{name}" if name in self.tools else None),
        ):
            patcher = mock.patch(target, value)
            patcher.start()
            testcase.addCleanup(patcher.stop)
        return self

    def called(self, *prefix: str) -> bool:
        return any(c[: len(prefix)] == list(prefix) for c in self.calls)

    def matching(self, *prefix: str) -> List[List[str]]:
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


def as_root(testcase) -> None:
    patcher = mock.patch("os.geteuid", return_value=0)
    patcher.start()
    testcase.addCleanup(patcher.stop)
