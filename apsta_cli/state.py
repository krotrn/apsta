"""Runtime record of the hotspot that is currently up.

Stored in /run (tmpfs), so it can never outlive a reboot. It is written once
the hotspot is fully up and removed once it is fully down; ``stop`` uses it to
undo exactly what ``start`` did. It holds no secrets and is world-readable so
unprivileged status readers (GUI, ``apsta status``) can use it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Dict, List, Optional

from .core import fsutil, paths


@dataclass
class HotspotState:
    method: str  # strategy name, see net.strategies
    base_interface: str
    ap_interface: str
    ssid: str
    channel: int
    band: str
    same_channel_required: bool
    sta_ssid_at_start: Optional[str] = None
    started_at: str = ""
    # hostapd strategy
    subnet: Optional[str] = None
    gateway: Optional[str] = None
    supervisor: Optional[str] = None
    firewall: Dict = field(default_factory=dict)
    # nmcli strategies
    connection_id: Optional[str] = None
    # client management
    client_limits: Dict[str, Dict[str, int]] = field(default_factory=dict)  # mac -> {pref, kbps}
    blocked: List[str] = field(default_factory=list)
    allowed_macs: List[str] = field(default_factory=list)  # hostapd MAC allowlist; empty = none

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> HotspotState:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def load() -> Optional[HotspotState]:
    try:
        data = json.loads(paths.STATE_PATH.read_text(encoding="utf-8"))
        return HotspotState.from_dict(data)
    except (FileNotFoundError, PermissionError):
        return None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def save(state: HotspotState) -> None:
    paths.ensure_run_dir()
    fsutil.atomic_write(paths.STATE_PATH, json.dumps(state.to_dict(), indent=2) + "\n", mode=0o644)


def clear() -> None:
    fsutil.remove(paths.STATE_PATH)


def interface_exists(name: Optional[str]) -> bool:
    return bool(name) and (paths.SYSFS_NET / name).exists()
