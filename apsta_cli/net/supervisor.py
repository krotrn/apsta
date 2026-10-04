"""Run hostapd/dnsmasq so something notices when they die.

* ``SystemdSupervisor`` launches each daemon as a transient unit
  (``systemd-run``): restarted on failure, logs in the journal, and outside
  the caller's login session, so logging out of the desktop that ran
  ``pkexec apsta start`` doesn't kill it.
* ``PidfileSupervisor`` is the fallback for non-systemd systems: classic
  daemonisation with a pidfile, verified against /proc before signalling.
"""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ..core import fsutil, shell


@dataclass
class Daemon:
    name: str  # also the executable's basename
    foreground: List[str]  # argv when a supervisor keeps it in the foreground
    background: List[str]  # argv that daemonises and writes ``pidfile``
    pidfile: Path

    @property
    def unit(self) -> str:
        return f"apsta-{self.name}.service"


class SystemdSupervisor:
    kind = "systemd"

    def start(self, d: Daemon) -> None:
        shell.run(["systemctl", "stop", d.unit])
        shell.run(["systemctl", "reset-failed", d.unit])
        shell.run(
            [
                "systemd-run",
                f"--unit={d.unit}",
                "--collect",
                "--quiet",
                f"--description=apsta {d.name}",
                "-p",
                "Restart=on-failure",
                "-p",
                "RestartSec=2",
                "--",
                *d.foreground,
            ]
        ).check(f"Starting {d.name}")

    def stop(self, d: Daemon) -> None:
        shell.run(["systemctl", "stop", d.unit], timeout=15)

    def running(self, d: Daemon) -> bool:
        return shell.run(["systemctl", "is-active", "--quiet", d.unit]).ok

    def logs(self, d: Daemon) -> str:
        return shell.out(["journalctl", "-u", d.unit, "-n", "8", "--no-pager", "-o", "cat"])


def _pid_matches(pid: int, name: str) -> bool:
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    # Match the program, or the script run by an interpreter (argv[1]).
    return any(os.path.basename(a.decode(errors="replace")) == name for a in argv[:2] if a)


class PidfileSupervisor:
    kind = "pidfile"

    def _pid(self, d: Daemon) -> Optional[int]:
        try:
            pid = int(d.pidfile.read_text().strip())
        except (OSError, ValueError):
            return None
        return pid if _pid_matches(pid, d.name) else None

    def start(self, d: Daemon) -> None:
        self.stop(d)
        shell.run(d.background, daemonizes=True).check(f"Starting {d.name}")

    def stop(self, d: Daemon) -> None:
        pid = self._pid(d)
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(30):
                    if not _pid_matches(pid, d.name):
                        break
                    time.sleep(0.1)
                else:
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        fsutil.remove(d.pidfile)

    def running(self, d: Daemon) -> bool:
        return self._pid(d) is not None

    def logs(self, d: Daemon) -> str:
        return ""


def get(kind: Optional[str] = None):
    """The supervisor named ``kind``, or the best available one."""
    kind = kind or os.environ.get("APSTA_SUPERVISOR")
    if kind == "systemd":
        return SystemdSupervisor()
    if kind == "pidfile":
        return PidfileSupervisor()
    if Path("/run/systemd/system").is_dir() and shell.have("systemd-run"):
        return SystemdSupervisor()
    return PidfileSupervisor()
