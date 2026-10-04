"""Every filesystem location apsta touches, in one place.

Configuration is persistent and lives in /etc. Runtime state lives in /run,
which is a tmpfs: a reboot or crash can never leave a stale "hotspot is
running" record behind. Environment overrides exist for tests and packaging.

Always reference these as ``paths.NAME`` (not ``from paths import NAME``) so
tests can patch them in one place.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


# Persistent configuration (root-owned). config.json is world-readable so the
# GUI and `apsta status` work unprivileged; passwords live in secrets.json (0600).
CONFIG_DIR = _env("APSTA_CONFIG_DIR", "/etc/apsta")
CONFIG_PATH = CONFIG_DIR / "config.json"
SECRETS_PATH = CONFIG_DIR / "secrets.json"

# Volatile runtime state (tmpfs).
RUN_DIR = _env("APSTA_RUN_DIR", "/run/apsta")
STATE_PATH = RUN_DIR / "state.json"
LOCK_PATH = RUN_DIR / "lock"
RESUME_MARKER = RUN_DIR / "resume-pending"
HOSTAPD_CONF = RUN_DIR / "hostapd.conf"
HOSTAPD_CTRL_DIR = RUN_DIR / "hostapd"
HOSTAPD_PID = RUN_DIR / "hostapd.pid"
DNSMASQ_CONF = RUN_DIR / "dnsmasq.conf"
DNSMASQ_PID = RUN_DIR / "dnsmasq.pid"
DNSMASQ_LEASES = RUN_DIR / "dnsmasq.leases"

# NetworkManager reads volatile keyfiles from /run too, so nmcli-mode
# connections disappear on reboot instead of accumulating in /etc.
NM_RUNTIME_KEYFILE_DIR = _env("APSTA_NM_KEYFILE_DIR", "/run/NetworkManager/system-connections")
NM_RUNTIME_CONF_DIR = _env("APSTA_NM_CONF_DIR", "/run/NetworkManager/conf.d")

LOG_PATH = _env("APSTA_LOG_PATH", "/var/log/apsta.log")

# Kernel interfaces (overridable so integration tests can run without hardware).
SYSFS_NET = _env("APSTA_SYSFS_NET", "/sys/class/net")
IP_FORWARD = _env("APSTA_IP_FORWARD", "/proc/sys/net/ipv4/ip_forward")


def ensure_run_dir() -> Path:
    """Create the runtime dir: root-writable, world-readable (state has no secrets)."""
    RUN_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    return RUN_DIR
