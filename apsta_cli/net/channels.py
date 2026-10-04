"""Channel/frequency maths and choosing the AP channel (pure functions)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, Optional, Tuple

from ..core.errors import HardwareError

# Channels that need radar detection (DFS) before transmitting as an AP.
DFS_CHANNELS = frozenset(list(range(52, 65, 4)) + list(range(100, 145, 4)))
SAFE_24G = (1, 6, 11)
SAFE_5G = (36, 40, 44, 48)


@dataclass(frozen=True)
class Channel:
    number: int
    band: str  # "bg" (2.4 GHz), "a" (5 GHz) or "6g"

    @property
    def label(self) -> str:
        return {"bg": "2.4 GHz", "a": "5 GHz", "6g": "6 GHz"}[self.band]

    @property
    def is_dfs(self) -> bool:
        return self.band == "a" and self.number in DFS_CHANNELS


@dataclass(frozen=True)
class ChannelPlan:
    channel: Channel
    reason: str


def from_freq(freq: int) -> Optional[Channel]:
    if freq == 2484:
        return Channel(14, "bg")
    if 2412 <= freq <= 2472 and (freq - 2407) % 5 == 0:
        return Channel((freq - 2407) // 5, "bg")
    if 5160 <= freq <= 5885 and freq % 5 == 0:
        return Channel((freq - 5000) // 5, "a")
    if 5955 <= freq <= 7115 and (freq - 5950) % 5 == 0:
        return Channel((freq - 5950) // 5, "6g")
    return None


def least_congested(band: str, scan: Iterable[Tuple[int, int]]) -> Optional[int]:
    """Pick the non-overlapping channel with the lowest weighted neighbour count.

    ``scan`` yields (channel, signal 0-100) for visible networks.
    """
    candidates = SAFE_5G if band == "a" else SAFE_24G
    scores: Dict[int, float] = {ch: 0.0 for ch in candidates}
    seen = False
    for channel, signal in scan:
        if channel in scores:
            seen = True
            scores[channel] += max(1.0, signal / 25.0)  # strong neighbours weigh more
    if not seen:
        return None
    return min(scores, key=lambda ch: (scores[ch], ch))


def allowed_channels(frequencies: Iterable[int]) -> Optional[FrozenSet[Channel]]:
    """Channels from the card's AP-capable frequencies; None when unknown (no data)."""
    channels = frozenset(c for c in (from_freq(f) for f in frequencies) if c is not None)
    return channels or None


def plan(
    sta: Optional[Channel],
    same_channel_required: bool,
    band: str,
    configured_channel: Optional[str],
    scan: Iterable[Tuple[int, int]] = (),
    allowed: Optional[FrozenSet[Channel]] = None,
) -> ChannelPlan:
    """Decide which channel the AP uses.

    On single-channel radios the AP *must* share the STA's channel; anything
    else either fails with EBUSY or knocks the STA off its network.
    """
    if sta is not None and same_channel_required:
        if sta.band == "6g":
            raise HardwareError(
                f"Your WiFi is connected on 6 GHz (channel {sta.number}), and this card can "
                "only run the hotspot on the same channel. Linux drivers don't allow AP mode on 6 GHz.",
                hints=["Connect to the 2.4 GHz or 5 GHz network of your router, then retry."],
            )
        if sta.is_dfs:
            raise HardwareError(
                f"Your WiFi is connected on DFS channel {sta.number}, and this card can only run "
                "the hotspot on the same channel. AP mode needs radar detection there, which "
                "client cards don't do.",
                hints=[
                    "Connect to a 2.4 GHz network, or a 5 GHz one on channel 36–48 or 149–165,",
                    "or ask the router admin to move off channels 52–144.",
                ],
            )
        if allowed is not None and sta not in allowed:
            raise HardwareError(
                f"Your WiFi is connected on {sta.label} channel {sta.number}, where this card isn't allowed "
                'to start a network (marked "no IR" by its firmware/regulatory rules). It can only run '
                "the hotspot on the same channel as your WiFi.",
                hints=[
                    "Switch the network you're connected to to 2.4 GHz (e.g. your phone's hotspot: AP band 2.4 GHz),",
                    "or connect to a 2.4 GHz network,",
                    "or run with --allow-disconnect to drop WiFi and host on an allowed channel.",
                ],
            )
        return ChannelPlan(sta, "matches the WiFi connection (single-channel radio)")

    if allowed is not None and not any(c.band == band for c in allowed):
        band = "bg"  # e.g. all of 5 GHz is "no IR" on this card
    default = 36 if band == "a" else 6
    try:
        fixed = int(configured_channel) if configured_channel else default
    except ValueError:
        fixed = default
    usable = None if allowed is None else {c.number for c in allowed if c.band == band}
    candidates = [(ch, sig) for ch, sig in scan if usable is None or ch in usable]
    picked = least_congested(band, candidates)
    if picked is not None:
        return ChannelPlan(Channel(picked, band), "least congested nearby")
    if usable is not None and fixed not in usable:
        safe = [ch for ch in (SAFE_5G if band == "a" else SAFE_24G) if ch in usable] or sorted(usable)
        return ChannelPlan(Channel(safe[0], band), "first channel this card may host on")
    return ChannelPlan(Channel(fixed, band), "configured default")


def parse_nmcli_scan(text: str) -> Iterable[Tuple[int, int]]:
    """Parse ``nmcli -t -f CHAN,SIGNAL device wifi list`` output."""
    for line in text.splitlines():
        chan, _, signal = line.partition(":")
        if chan.strip().isdigit():
            yield int(chan), int(signal) if signal.strip().isdigit() else 40
