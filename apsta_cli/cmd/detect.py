"""detect: what can this machine's WiFi hardware do, and which method will apsta use."""

from __future__ import annotations

import json

from ..core import output
from ..core.output import C
from ..services import detect


def cmd_detect(args) -> int:
    found = detect.report()
    if args.json:
        print(json.dumps(found.to_dict(), indent=2))
        return 0

    ifaces, target, cap, result = found.ifaces, found.target, found.cap, found.verdict
    output.head("apsta — Hardware Detection")
    output.blank()
    output.info(f"Found {len(ifaces)} WiFi interface(s):")
    for i in ifaces:
        if i.connected_ssid:
            link = f"connected to {C.GREEN}{i.connected_ssid}{C.RESET}"
        else:
            link = f"{C.DIM}not connected{C.RESET}"
        output.detail(f"{C.BOLD}{i.name}{C.RESET}  [{i.mac}]  {link}")

    output.head(f"Capability report for {target.name}")
    if cap.driver:
        output.info(f"Driver:   {cap.driver}")
    if cap.chipset:
        output.info(f"Chipset:  {cap.chipset}")
    output.blank()
    _row("AP mode (hotspot)", cap.supports_ap)
    _row("STA mode (WiFi client)", cap.supports_sta)
    _row("AP + STA at the same time", cap.ap_sta)
    if cap.ap_sta:
        _row("AP on a different channel than STA", not cap.same_channel_required)
        if cap.same_channel_required:
            _row("Wi-Fi Direct group on its own channel", cap.p2p_go_own_channel)
    _row("Hotspot on 5 GHz", any(f >= 5000 for f in cap.ap_frequencies))
    if cap.combinations:
        output.blank()
        output.info("Interface combinations reported by the driver:")
        for combo in cap.combinations:
            output.detail(f"{C.DIM}{combo}{C.RESET}")

    output.head("Methods")
    for name, status in found.methods.items():
        colour = C.GREEN if status == "ready" else C.YELLOW
        output.detail(f"{name:<8} {colour}{status}{C.RESET}")

    output.head("Verdict")
    say = output.ok if result["mode"] == "ap+sta" else {"warn": output.warn}.get(result["level"], output.err)
    for msg in result["messages"]:
        say(msg)
    for msg in result.get("warnings", []):
        output.warn(msg)
    if result["mode"] == "single":
        capable = detect.capable_usb_adapters()
        if capable:
            output.ok("A compatible USB adapter is plugged in:")
            for dev, chipset in capable:
                output.detail(f"{chipset.chipset}  iface: {dev.interface or 'not yet assigned'}")
            output.info("Use it: sudo apsta config --set interface=<iface>")
        else:
            output.info("Keep WiFi with a USB adapter: apsta recommend")
    output.info(f"Next: {result['next']}")
    output.blank()
    return 0


def _row(label: str, value: bool) -> None:
    icon = f"{C.GREEN}✔{C.RESET}" if value else f"{C.RED}✘{C.RESET}"
    text = f"{C.GREEN}yes{C.RESET}" if value else f"{C.RED}no{C.RESET}"
    output.detail(f"{icon}  {label:<36} {text}")
