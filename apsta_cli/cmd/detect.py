"""detect: what can this machine's WiFi hardware do, and which method will apsta use."""

from __future__ import annotations

import json

from ..core import output, shell
from ..core.errors import HardwareError
from ..core.output import C
from ..hw import capability, interfaces, usb


def verdict(cap: capability.HardwareCapability) -> dict:
    if cap.ap_sta:
        messages = ["Your card can run a hotspot while staying connected to WiFi."]
        if cap.same_channel_required:
            messages.append("The hotspot will use the same channel as your WiFi connection.")
        return {"level": "ok", "mode": "ap+sta", "messages": messages, "next": "sudo apsta start"}
    if cap.supports_ap:
        return {
            "level": "warn",
            "mode": "single",
            "messages": [
                "Your card supports AP mode but not alongside a WiFi connection.",
                "Starting a hotspot will disconnect your WiFi.",
            ],
            "next": "sudo apsta start --allow-disconnect",
        }
    return {
        "level": "error",
        "mode": "unsupported",
        "messages": ["Your card does not support AP mode.", "A USB WiFi adapter is required."],
        "next": "apsta recommend",
    }


def methods() -> dict:
    missing = [b for b in ("hostapd", "dnsmasq") if not shell.have(b)]
    return {
        "hostapd": "ready" if not missing else f"needs {', '.join(missing)}",
        "nmcli": "ready" if shell.have("nmcli") else "needs NetworkManager",
    }


def cmd_detect(args) -> int:
    ifaces = interfaces.client_interfaces()
    if not ifaces:
        raise HardwareError("No WiFi interfaces found.")
    target = next((i for i in ifaces if i.connected_ssid), None) or next(
        (i for i in ifaces if i.state == "UP"), ifaces[0]
    )
    cap = capability.probe(target.name)
    result = verdict(cap)
    available = methods()

    if args.json:
        print(
            json.dumps(
                {
                    "interfaces": [i.__dict__ for i in ifaces],
                    "target_interface": target.name,
                    "capability": cap.to_dict(),
                    "methods": available,
                    "verdict": result,
                },
                indent=2,
            )
        )
        return 0

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
    if cap.combinations:
        output.blank()
        output.info("Interface combinations reported by the driver:")
        for combo in cap.combinations:
            output.detail(f"{C.DIM}{combo}{C.RESET}")

    output.head("Methods")
    for name, status in available.items():
        colour = C.GREEN if status == "ready" else C.YELLOW
        output.detail(f"{name:<8} {colour}{status}{C.RESET}")

    output.head("Verdict")
    for msg in result["messages"]:
        {"ok": output.ok, "warn": output.warn}.get(result["level"], output.err)(msg)
    if result["mode"] == "single":
        capable = [d for d in usb.scan_usb_wifi() if d.chipset_db and d.chipset_db.ap_sta]
        if capable:
            output.ok("A compatible USB adapter is plugged in:")
            for dev in capable:
                output.detail(f"{dev.chipset_db.chipset}  iface: {dev.interface or 'not yet assigned'}")
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
