#!/usr/bin/env python3
"""Shared constants and process helpers for the GTK UI."""

import json
import os
import shutil
import subprocess
from pathlib import Path

APP_ID = "com.github.apsta.Gtk"
APSTA = shutil.which("apsta") or "/usr/local/bin/apsta"
CONFIG = Path("/etc/apsta/config.json")
VERSION = "0.6.0"

# Background poll interval in seconds — keeps status in sync with daemon
POLL_INTERVAL = 5


def read_config() -> dict:
    """Read /etc/apsta/config.json. Returns {} on any error."""
    try:
        cfg = json.loads(CONFIG.read_text())
        profiles = cfg.get("profiles")
        active = cfg.get("active_profile")
        if isinstance(profiles, dict) and isinstance(active, str) and active in profiles:
            selected = profiles.get(active) or {}
            for key in ("ssid", "password", "band", "channel", "interface"):
                if key in selected:
                    cfg[key] = selected.get(key)
        return cfg
    except (OSError, json.JSONDecodeError):
        return {}


def run_apsta(*args: str) -> tuple[int, str, str]:
    """
    Run apsta with the given arguments. Returns (returncode, stdout, stderr).
    Does NOT use pkexec — for read-only commands (detect, status, scan-usb).
    """
    try:
        r = subprocess.run(
            [APSTA, *args],
            capture_output=True,
            text=True,
            env={**os.environ, "NO_COLOR": "1"},
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"apsta not found at {APSTA}"


def run_apsta_root_script(script: str, *positional: str) -> tuple[int, str, str]:
    """
    Run a shell script via pkexec, passing user-supplied values as positional
    arguments ($1, $2, ...) rather than interpolating them into the script.
    """
    try:
        r = subprocess.run(
            ["pkexec", "sh", "-c", script, "--", *positional],
            capture_output=True,
            text=True,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return 127, "", "pkexec not found — cannot escalate privileges"


def pkexec_error_message(returncode: int, stderr: str, stdout: str = "") -> str:
    if returncode == 126:
        return "Authentication cancelled."
    if returncode == 127:
        return "pkexec or apsta not found. Is apsta installed?"
    raw = stderr or stdout or "Unknown error"
    return strip_ansi(raw).strip()[:200]


def strip_ansi(s: str) -> str:
    result = []
    i = 0
    while i < len(s):
        if s[i] == "\x1b" and i + 1 < len(s) and s[i + 1] == "[":
            i += 2
            while i < len(s) and s[i] != "m":
                i += 1
            i += 1
        else:
            result.append(s[i])
            i += 1
    return "".join(result)


def first_error_line(text: str) -> str:
    lines = text.splitlines()
    for line in lines:
        stripped = line.strip()
        if any(marker in stripped for marker in ("✘", "⚠", "Error", "error", "failed", "Failed")):
            clean = stripped.lstrip("✘⚠ ").strip()
            if clean:
                return clean

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("→"):
            continue
        if "—" in stripped:
            continue
        return stripped

    return text[:120]
