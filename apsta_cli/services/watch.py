"""``apsta run``: start the hotspot and keep it healthy (what the service runs).

Every few seconds the watcher checks that:

* the hotspot is still up (hostapd alive, AP interface present) — it isn't
  after suspend/resume or a driver reset;
* on single-channel radios, the WiFi connection hasn't moved to another
  channel (router channel change, roaming) — the AP must follow it;
* on single-channel radios, the WiFi connection hasn't been lost for long —
  the AP may be what blocks NetworkManager from reconnecting elsewhere.

If any check fails it tears the hotspot down, lets the WiFi settle and starts
it again. The decision logic is the pure function :func:`decide`.
"""

from __future__ import annotations

import signal
import threading
import time
from dataclasses import dataclass
from typing import Optional

from .. import state as state_store
from ..core import output
from ..core.errors import AlreadyRunning, ApstaError
from ..hw import interfaces
from ..net import channels
from ..state import HotspotState
from . import hotspot

POLL_SECONDS = 5.0
STA_LOST_GRACE = 20.0
RETRY_MIN, RETRY_MAX = 10.0, 300.0


@dataclass
class Observation:
    state_present: bool
    alive: bool
    sta_channel: Optional[channels.Channel]


@dataclass
class Memory:
    sta_lost_since: Optional[float] = None


def decide(st: Optional[HotspotState], obs: Observation, now: float, mem: Memory) -> Optional[str]:
    """Return "exit", a restart reason, or None to keep going."""
    if st is None or not obs.state_present:
        return "exit"
    if not obs.alive:
        return "the hotspot went down"
    if not st.same_channel_required:
        mem.sta_lost_since = None
        return None
    if obs.sta_channel is not None:
        mem.sta_lost_since = None
        if obs.sta_channel.number != st.channel:
            return f"WiFi moved to channel {obs.sta_channel.number}"
        return None
    if st.sta_ssid_at_start:
        mem.sta_lost_since = mem.sta_lost_since or now
        if now - mem.sta_lost_since >= STA_LOST_GRACE:
            mem.sta_lost_since = None
            return "the WiFi connection was lost"
    return None


def observe(st: Optional[HotspotState]) -> Observation:
    if st is None:
        return Observation(False, False, None)
    link = interfaces.sta_link(st.base_interface)
    return Observation(True, hotspot.is_alive(st), channels.from_freq(link.freq) if link else None)


class Watcher:
    def __init__(self, opts: hotspot.StartOptions, poll: float = POLL_SECONDS):
        self.opts = opts
        self.poll = poll
        self.stop_event = threading.Event()

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, lambda *_: self.stop_event.set())

    def _start(self) -> bool:
        try:
            result = hotspot.start(self.opts)
        except AlreadyRunning:
            output.info("Adopting the hotspot that is already running.")
            return True
        except ApstaError as exc:
            output.err(exc.message)
            for hint in exc.hints:
                output.hint(hint)
            return False
        output.ok(f"Hotspot '{result.state.ssid}' up on {result.state.ap_interface} (channel {result.state.channel})")
        return True

    def _start_with_retry(self) -> bool:
        delay = RETRY_MIN
        while not self.stop_event.is_set():
            if self._start():
                return True
            output.info(f"Retrying in {delay:.0f}s")
            if self.stop_event.wait(delay):
                return False
            delay = min(delay * 2, RETRY_MAX)
        return False

    def run(self) -> int:
        if not self._start_with_retry():
            return 0
        mem = Memory()
        while not self.stop_event.wait(self.poll):
            st = state_store.load()
            action = decide(st, observe(st), time.monotonic(), mem)
            if action == "exit":
                output.info("Hotspot was stopped; exiting.")
                return 0
            if action:
                output.warn(f"Restarting the hotspot: {action}.")
                hotspot.stop()
                if not self._start_with_retry():
                    break
        hotspot.stop()
        return 0
