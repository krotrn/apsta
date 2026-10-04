"""The GUI's only way to talk to apsta: the CLI, with JSON for data.

Keeping the GUI a client of the CLI means one implementation of every
operation and one privilege boundary: privileged calls run
``pkexec /path/to/apsta <args>`` (covered by the com.github.apsta.manage
polkit action) — never a root shell. Secrets go over stdin, never argv.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional

APSTA = shutil.which("apsta") or "/usr/bin/apsta"


@dataclass
class Result:
    ok: bool
    message: str
    stdout: str = ""


def _strip_ansi(text: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _first_error(text: str) -> str:
    for line in text.splitlines():
        line = line.strip().lstrip("✘⚠ ").strip()
        if line:
            return line[:200]
    return "Unknown error"


def _run(argv: List[str], stdin: Optional[str] = None, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "NO_COLOR": "1"},
    )


class ApstaBackend:
    def __init__(self, apsta: str = APSTA):
        self.apsta = apsta

    def available(self) -> bool:
        return os.path.isfile(self.apsta) and os.access(self.apsta, os.X_OK)

    # ── unprivileged ──────────────────────────────────────────────────────────

    def status(self) -> dict:
        try:
            proc = _run([self.apsta, "status", "--json"], timeout=20)
            return json.loads(proc.stdout) if proc.returncode == 0 else {}
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return {}

    def text(self, *args: str) -> str:
        try:
            proc = _run([self.apsta, *args], timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            return str(exc)
        return _strip_ansi(proc.stdout + proc.stderr).strip()

    # ── privileged ────────────────────────────────────────────────────────────

    def privileged(self, *args: str, stdin: Optional[str] = None, success: str = "Done.") -> Result:
        try:
            proc = _run(["pkexec", self.apsta, *args], stdin=stdin)
        except FileNotFoundError:
            return Result(False, "pkexec not found; install polkit.")
        except subprocess.TimeoutExpired:
            return Result(False, "The operation timed out.")
        if proc.returncode == 0:
            return Result(True, success, proc.stdout)
        if proc.returncode in (126, 127) and not proc.stdout:
            return Result(False, "Authentication cancelled." if proc.returncode == 126 else "apsta not found.")
        return Result(False, _first_error(_strip_ansi(proc.stderr or proc.stdout)))

    def start(self, ssid: str, password: str, allow_disconnect: bool) -> Result:
        args = ["start", "--ssid", ssid]
        if password:
            args.append("--password-stdin")
        if allow_disconnect:
            args.append("--allow-disconnect")
        return self.privileged(*args, stdin=password + "\n" if password else None, success="Hotspot started.")

    def stop(self) -> Result:
        return self.privileged("stop", success="Hotspot stopped.")

    def save_config(self, ssid: str, password: str, interface: str) -> Result:
        args = ["config", "--set", f"ssid={ssid}", "--set", f"interface={interface or 'auto'}"]
        if password:
            args.append("--password-stdin")
        return self.privileged(*args, stdin=password + "\n" if password else None, success="Configuration saved.")

    def use_profile(self, name: str) -> Result:
        return self.privileged("profile", "use", name, success=f"Switched to profile '{name}'.")

    def disconnect(self, client: str, block: bool = False) -> Result:
        args = ["clients", "disconnect", client] + (["--block"] if block else [])
        return self.privileged(*args, success="Client blocked." if block else "Client disconnected.")

    def limit(self, client: str, kbps: int) -> Result:
        return self.privileged("clients", "limit", client, str(kbps), success=f"Applied {kbps} Kbps limit.")

    def enable_service(self) -> Result:
        return self.privileged("enable", success="Auto-start enabled.")

    def disable_service(self) -> Result:
        return self.privileged("disable", success="Auto-start disabled.")
