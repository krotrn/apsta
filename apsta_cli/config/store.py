"""Loading and saving configuration.

* config.json — world-readable, no secrets.
* secrets.json — 0600, profile name -> password.

Both are written atomically, secrets first, so a crash can lose at most the
change being made. A corrupt config.json is backed up rather than silently
replaced with defaults, so profiles are never destroyed by a bad write.
"""

from __future__ import annotations

import json
import os
import secrets as _secrets
import string
from copy import deepcopy
from datetime import datetime, timezone
from typing import Optional

from ..core import fsutil, output, paths
from . import model


class _Unreadable:
    """Sentinel: the secrets file exists but we aren't allowed to read it."""


UNREADABLE = _Unreadable()


def _read_json(path) -> Optional[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else None


def _load_secrets():
    try:
        return _read_json(paths.SECRETS_PATH) or {}
    except FileNotFoundError:
        return {}
    except PermissionError:
        return UNREADABLE
    except (json.JSONDecodeError, UnicodeDecodeError):
        output.warn(f"{paths.SECRETS_PATH} is corrupted; passwords must be set again.")
        return {}


def _backup_corrupt(path) -> None:
    if os.geteuid() != 0:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        os.replace(path, backup)
        output.warn(f"Moved the unreadable file to {backup}")
    except OSError:
        pass  # couldn't move it aside; the warning above still tells the user


def load() -> dict:
    """Load the config. Passwords are ``None`` when the caller can't read secrets."""
    raw: dict = {}
    try:
        raw = _read_json(paths.CONFIG_PATH) or {}
    except FileNotFoundError:
        pass  # first run: no config yet, defaults apply
    except (json.JSONDecodeError, UnicodeDecodeError):
        output.warn(f"{paths.CONFIG_PATH} is corrupted; using defaults.")
        _backup_corrupt(paths.CONFIG_PATH)

    config = model.normalize(raw)
    stored = _load_secrets()
    for name, profile in config["profiles"].items():
        if stored is UNREADABLE:
            profile["password"] = None
        elif isinstance(stored.get(name), str) and stored[name] not in model.INSECURE_PASSWORDS:
            profile["password"] = stored[name]
    config["password"] = config["profiles"][config["active_profile"]]["password"]

    # Migrate files written by older versions (plaintext passwords, runtime keys).
    needs_migration = model.had_legacy_runtime_keys(raw) or _has_plaintext_password(raw)
    if needs_migration and stored is not UNREADABLE and os.geteuid() == 0:
        save(config)
    return config


def _has_plaintext_password(raw: dict) -> bool:
    profiles = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else {}
    return "password" in raw or any(isinstance(p, dict) and "password" in p for p in profiles.values())


def save(config: dict) -> None:
    config = model.normalize(config)
    paths.CONFIG_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)

    stored = {name: p["password"] for name, p in config["profiles"].items() if p.get("password")}
    fsutil.atomic_write(paths.SECRETS_PATH, json.dumps(stored, indent=2) + "\n", mode=0o600)

    public = deepcopy(config)
    public.pop("password", None)
    for profile in public["profiles"].values():
        profile.pop("password", None)
    fsutil.atomic_write(paths.CONFIG_PATH, json.dumps(public, indent=2) + "\n", mode=0o644)


def generate_password(length: int = 16) -> str:
    """Random WPA2 passphrase without look-alike characters, easy to type on a phone."""
    alphabet = "".join(c for c in string.ascii_letters + string.digits if c not in "Il1O0o")
    return "".join(_secrets.choice(alphabet) for _ in range(length))
