"""The GUI's only way to talk to apsta: the CLI, with JSON for data.

Keeping the GUI a client of the CLI means one implementation of every
operation and one privilege boundary: privileged calls run
``pkexec /path/to/apsta <args>`` (covered by the com.github.apsta.manage
polkit action), never a root shell. Secrets travel over stdin, never argv.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

APSTA = shutil.which("apsta") or "/usr/bin/apsta"


@dataclass
class Result:
    ok: bool
    message: str
    data: dict = field(default_factory=dict)


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def first_error(text: str) -> str:
    for line in strip_ansi(text).splitlines():
        line = line.strip().lstrip("✘⚠ ").strip()
        if line:
            return line[:200]
    return "Unknown error"


def pkexec_failure(returncode: int, stderr: str) -> Optional[str]:
    """Explain failures that come from pkexec itself rather than apsta."""
    low = stderr.lower()
    if "no authentication agent" in low:
        return "No polkit authentication agent is running. Start one (e.g. polkit-gnome) or use the terminal."
    if returncode == 126:
        return "Authentication cancelled."
    if returncode == 127 and ("not authorized" in low or "authorization" in low or not stderr.strip()):
        return "Not authorized."
    return None


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

    def _json(self, *args: str) -> dict:
        try:
            proc = _run([self.apsta, *args], timeout=30)
            return json.loads(proc.stdout) if proc.returncode == 0 else {}
        except (OSError, subprocess.SubprocessError, ValueError):
            return {}

    def status(self) -> dict:
        return self._json("status", "--json")

    def detect(self) -> dict:
        return self._json("detect", "--json")

    def text(self, *args: str) -> str:
        try:
            proc = _run([self.apsta, *args], timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            return str(exc)
        return strip_ansi(proc.stdout + proc.stderr).strip()

    # ── privileged ────────────────────────────────────────────────────────────

    def privileged(self, *args: str, stdin: Optional[str] = None, success: str = "Done.") -> Result:
        try:
            proc = _run(["pkexec", self.apsta, *args], stdin=stdin)
        except FileNotFoundError:
            return Result(False, "pkexec not found; install polkit.")
        except subprocess.TimeoutExpired:
            return Result(False, "The operation timed out.")
        if proc.returncode == 0:
            return Result(True, success, {"stdout": proc.stdout})
        if not proc.stdout:
            reason = pkexec_failure(proc.returncode, proc.stderr)
            if reason:
                return Result(False, reason)
        return Result(False, first_error(proc.stderr or proc.stdout))

    def start(self, allow_disconnect: bool = False) -> Result:
        args = ["start"] + (["--allow-disconnect"] if allow_disconnect else [])
        return self.privileged(*args, success="Hotspot started.")

    def stop(self) -> Result:
        return self.privileged("stop", success="Hotspot stopped.")

    def secrets(self) -> Result:
        """Active profile settings including the password (asks for authentication)."""
        result = self.privileged("config", "--json", "--show-password", success="")
        if result.ok:
            try:
                result.data = json.loads(result.data["stdout"])
            except (KeyError, ValueError):
                return Result(False, "Could not read the configuration.")
        return result

    def save_config(
        self,
        ssid: str,
        password: str,
        band: str,
        interface: str,
        hidden: bool = False,
        method: str = "auto",
        channel: str = "auto",
    ) -> Result:
        args = ["config", "--set", f"ssid={ssid}", "--set", f"band={band}", "--set", f"interface={interface or 'auto'}"]
        args += [
            "--set",
            f"hidden={'yes' if hidden else 'no'}",
            "--set",
            f"method={method}",
            "--set",
            f"channel={channel}",
        ]
        if password:
            args.append("--password-stdin")
        return self.privileged(*args, stdin=password + "\n" if password else None, success="Settings saved.")

    def set_config(self, changes: dict, success: str = "Settings saved.") -> Result:
        """Save some settings of the active profile, e.g. {"band": "a"}."""
        args = ["config"]
        for key, value in changes.items():
            args += ["--set", f"{key}={value}"]
        return self.privileged(*args, success=success)

    def use_profile(self, name: str) -> Result:
        return self.privileged("profile", "use", name, success=f"Using profile “{name}”.")

    def create_profile(self, name: str) -> Result:
        return self.privileged("profile", "create", name, success=f"Created profile “{name}”.")

    def disconnect(self, client: str, block: bool = False) -> Result:
        args = ["clients", "disconnect", client] + (["--block"] if block else [])
        return self.privileged(*args, success="Device blocked." if block else "Device disconnected.")

    def unblock(self, client: str) -> Result:
        return self.privileged("clients", "unblock", client, success="Device unblocked.")

    def limit(self, client: str, kbps: int) -> Result:
        return self.privileged("clients", "limit", client, str(kbps), success="Speed limit applied.")

    def unlimit(self, client: str) -> Result:
        return self.privileged("clients", "unlimit", client, success="Speed limit removed.")

    def set_autostart(self, enabled: bool) -> Result:
        if enabled:
            return self.privileged("enable", success="The hotspot will start automatically.")
        return self.privileged("disable", success="Automatic start turned off.")
