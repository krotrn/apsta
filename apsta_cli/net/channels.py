"""Channel/frequency maths and choosing the AP channel (pure functions)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

from ..core.errors import HardwareError

# Channels that need radar detection (DFS) before transmitting as an AP.
DFS_CHANNELS = frozenset(list(range(52, 65, 4)) + list(range(100, 145, 4)))
SAFE_24G = (1, 6, 11)
# Non-overlapping, no radar detection needed. Many cards may only start a
# network on one of the two groups (Intel often blocks 36-48), so both count.
SAFE_5G = (36, 40, 44, 48, 149, 153, 157, 161, 165)

# User guide for "can't host on this channel" (no IR); linked from errors.
DOCS_5GHZ = "https://github.com/krotrn/apsta/blob/main/docs/5ghz-wifi.md"


@dataclass(frozen=True)
class Channel:
    number: int
    band: str  # "bg" (2.4 GHz), "a" (5 GHz) or "6g"

    @property
    def label(self) -> str:
        return {"bg": "2.4 GHz", "a": "5 GHz", "6g": "6 GHz"}[self.band]

    @property
    def freq(self) -> int:
        """Centre frequency in MHz (inverse of :func:`from_freq`)."""
        if self.band == "bg":
            return 2484 if self.number == 14 else 2407 + 5 * self.number
        return (5000 if self.band == "a" else 5950) + 5 * self.number

    @property
    def is_dfs(self) -> bool:
        return self.band == "a" and self.number in DFS_CHANNELS


@dataclass(frozen=True)
class ChannelPlan:
    channel: Channel
    reason: str  # why this channel, in words for the user ("least crowded nearby")
    notes: Tuple[str, ...] = ()  # what the user asked for and didn't get, and why


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


def least_congested(
    band: str, scan: Iterable[Tuple[int, int]], usable: Optional[FrozenSet[int]] = None
) -> Optional[int]:
    """Pick the non-overlapping channel with the lowest weighted neighbour count.

    ``scan`` yields (channel, signal 0-100) for visible networks; ``usable``
    limits the choice to channels the card may start a network on.
    """
    candidates = [ch for ch in (SAFE_5G if band == "a" else SAFE_24G) if usable is None or ch in usable]
    if not candidates:
        return None
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


def valid_for_band(number: int, band: str) -> bool:
    if band == "bg":
        return 1 <= number <= 14
    return from_freq(5000 + 5 * number) == Channel(number, "a")


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
    else either fails with EBUSY or knocks the STA off its network. Otherwise
    the configured channel wins when the card may use it, then the least
    crowded safe channel. Anything asked for but not granted is explained in
    ``notes``.
    """
    if sta is not None and same_channel_required:
        _require_can_host(sta, allowed)
        return ChannelPlan(sta, "the same as your WiFi's (this card uses one channel for both)")
    return _own_channel(band, configured_channel, scan, allowed)


def _require_can_host(sta: Channel, allowed: Optional[FrozenSet[Channel]]) -> None:
    """Raise :class:`HardwareError` explaining why an AP can't share ``sta``'s channel, if it can't."""
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
                f"Why: {DOCS_5GHZ}",
            ],
        )


def _own_channel(
    band: str,
    configured_channel: Optional[str],
    scan: Iterable[Tuple[int, int]],
    allowed: Optional[FrozenSet[Channel]],
) -> ChannelPlan:
    """A channel of the hotspot's own: the configured one if allowed, else the least crowded."""
    notes: List[str] = []
    if allowed is not None and not any(c.band == band for c in allowed):
        notes.append(f"This card can't start a network on {Channel(1, band).label}, so the hotspot uses 2.4 GHz.")
        band = "bg"  # e.g. all of 5 GHz is "no IR" on this card
    usable = None if allowed is None else frozenset(c.number for c in allowed if c.band == band)

    wanted = int(configured_channel) if configured_channel and configured_channel.isdigit() else None
    if wanted is not None:
        refusal = _refusal(wanted, band, usable)
        if refusal is None:
            return ChannelPlan(Channel(wanted, band), "your channel setting", tuple(notes))
        notes.append(refusal)

    picked = least_congested(band, scan, usable)
    if picked is not None:
        return ChannelPlan(Channel(picked, band), "the least crowded nearby", tuple(notes))
    return ChannelPlan(Channel(_default(band, usable), band), "the default (no scan available)", tuple(notes))


def _refusal(wanted: int, band: str, usable: Optional[FrozenSet[int]]) -> Optional[str]:
    """Why the configured channel can't be used, or None if it can."""
    if not valid_for_band(wanted, band):
        label = Channel(1, band).label
        return (
            f"Your channel setting ({wanted}) isn't a {label} channel, so apsta picks one "
            f"(set channel to auto or a {label} channel)."
        )
    if usable is not None and wanted not in usable:
        return (
            f"This card isn't allowed to start a network on channel {wanted} "
            '(radar or "no IR" rules), so apsta picks another.'
        )
    return None


def _default(band: str, usable: Optional[FrozenSet[int]]) -> int:
    """Without a scan: 6 or 36 when the card may use it, else the first safe (or any usable) channel."""
    safe = [ch for ch in (SAFE_5G if band == "a" else SAFE_24G) if usable is None or ch in usable]
    if not safe and usable:
        safe = sorted(usable)
    default = 36 if band == "a" else 6
    return default if default in safe or not safe else safe[0]


def parse_nmcli_scan(text: str) -> Iterable[Tuple[int, int]]:
    """Parse ``nmcli -t -f CHAN,SIGNAL device wifi list`` output."""
    for line in text.splitlines():
        chan, _, signal = line.partition(":")
        if chan.strip().isdigit():
            yield int(chan), int(signal) if signal.strip().isdigit() else 40
