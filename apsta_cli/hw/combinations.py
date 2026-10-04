"""Parse and reason about nl80211 "valid interface combinations".

``iw phy <phy> info`` lists combinations like::

    * #{ managed } <= 1, #{ AP, P2P-client, P2P-GO } <= 1, #{ P2P-device } <= 1,
      total <= 3, #channels <= 1

Each ``#{ types } <= n`` is a group: at most ``n`` interfaces whose types are
in that set may exist at once. ``total`` bounds all interfaces together and
``#channels`` is how many distinct channels they may use simultaneously.

An AP and a STA can coexist when some combination admits one ``AP`` and one
``managed`` interface at the same time. Whether they share a group doesn't
matter, only the counts do. When ``#channels <= 1`` the AP has to run on the
STA's channel.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Tuple

_GROUP_RE = re.compile(r"#\{\s*([^}]*)\}\s*<=\s*(\d+)")
_TOTAL_RE = re.compile(r"total\s*<=\s*(\d+)")
_CHANNELS_RE = re.compile(r"#channels\s*<=\s*(\d+)")


@dataclass(frozen=True)
class Group:
    types: FrozenSet[str]
    limit: int


@dataclass(frozen=True)
class Combination:
    groups: Tuple[Group, ...]
    total: int
    channels: int
    raw: str

    def allows(self, wanted: Dict[str, int]) -> bool:
        """Can these interface types (type -> count) exist simultaneously?"""
        instances = [t for t, n in wanted.items() for _ in range(n)]
        if len(instances) > self.total:
            return False
        remaining = [g.limit for g in self.groups]

        def place(i: int) -> bool:
            if i == len(instances):
                return True
            for gi, group in enumerate(self.groups):
                if instances[i] in group.types and remaining[gi] > 0:
                    remaining[gi] -= 1
                    if place(i + 1):
                        return True
                    remaining[gi] += 1
            return False

        return place(0)

    @property
    def ap_sta(self) -> bool:
        return self.allows({"managed": 1, "AP": 1})


@dataclass(frozen=True)
class ApStaSupport:
    supported: bool
    same_channel_required: bool
    combination: Optional[Combination]


def parse_entry(text: str) -> Optional[Combination]:
    groups = tuple(
        Group(frozenset(t.strip() for t in types.split(",") if t.strip()), int(limit))
        for types, limit in _GROUP_RE.findall(text)
    )
    total = _TOTAL_RE.search(text)
    if not groups or not total:
        return None
    channels = _CHANNELS_RE.search(text)
    return Combination(
        groups=groups,
        total=int(total.group(1)),
        channels=int(channels.group(1)) if channels else 1,
        raw=" ".join(text.split()),
    )


def parse_combinations(iw_text: str) -> List[Combination]:
    """Extract combinations from ``iw phy info`` / ``iw list`` output."""
    entries: List[str] = []
    in_section = False
    for line in iw_text.splitlines():
        stripped = line.strip()
        if not in_section:
            in_section = stripped.startswith("valid interface combinations")
            continue
        if not stripped:
            continue
        if stripped.startswith("*") and "#{" in stripped:
            entries.append(stripped.lstrip("* ").strip())
        elif entries and (stripped.startswith(("total", "#")) or entries[-1].endswith(",")):
            entries[-1] += " " + stripped
        else:
            break  # next section of the iw output
    return [c for c in (parse_entry(e) for e in entries) if c]


def parse_supported_modes(iw_text: str) -> List[str]:
    modes: List[str] = []
    in_section = False
    for line in iw_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Supported interface modes"):
            in_section = True
            continue
        if in_section:
            if stripped.startswith("* "):
                modes.append(stripped[2:].strip())
            else:
                break
    return modes


def evaluate(combinations: List[Combination]) -> ApStaSupport:
    """Pick the most capable combination that allows AP+STA, preferring multi-channel."""
    capable = [c for c in combinations if c.ap_sta]
    if not capable:
        return ApStaSupport(False, True, None)
    best = max(capable, key=lambda c: (c.channels, c.total))
    return ApStaSupport(True, best.channels <= 1, best)
