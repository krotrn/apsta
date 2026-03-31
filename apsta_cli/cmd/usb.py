#!/usr/bin/env python3
"""USB scan and recommendation command implementations."""

from typing import List, Tuple

from ..common import C, head, info, ok, run_out, warn
from ..hardware import USB_CHIPSET_DB, UsbWifiDevice, get_hardware_capability, get_wifi_interfaces, scan_usb_wifi
def cmd_scan_usb(args):
    head("apsta — USB WiFi Adapter Scan")
    print()

    devices = scan_usb_wifi()

    if not devices:
        info("No USB WiFi adapters detected.")
        info("Plug in a USB adapter, then run:  apsta scan-usb")
        print()
        info("Don't have one? Run:  apsta recommend")
        print()
        return

    info(f"Found {len(devices)} USB WiFi device(s):")
    print()

    for dev in devices:
        cs = dev.chipset_db
        vid_pid = f"{dev.vid}:{dev.pid}"

        if cs:
            ap_sta_icon = f"{C.GREEN}✔ AP+STA{C.RESET}" if cs.ap_sta else f"{C.RED}✘ no AP+STA{C.RESET}"
            print(f"  {C.BOLD}{cs.chipset}{C.RESET}  [{vid_pid}]  {cs.wifi_gen}  {ap_sta_icon}")
            print(f"       Name:    {dev.name}")
            print(f"       Driver:  {dev.driver or C.DIM + 'not loaded' + C.RESET}")
            iface_display = dev.interface or f"{C.DIM}not assigned{C.RESET}"
            print(f"       Iface:   {iface_display}")
            print(f"       Kernel:  {cs.min_kernel}+ required for AP mode")
            if cs.notes:
                print(f"       Note:    {C.DIM}{cs.notes}{C.RESET}")

            if cs.ap_sta and dev.interface:
                print()
                ok(f"This adapter supports AP+STA. Use it as your hotspot interface:")
                info(f"  sudo apsta config --set interface={dev.interface}")
                info(f"  sudo apsta start")
            elif cs.ap_sta and not dev.interface:
                print()
                warn("Adapter is recognized but has no kernel interface assigned.")
                info("Two likely causes:")
                info("  1. Driver not loaded — check:  lsmod | grep " + (cs.driver or "mt7921u"))
                info("  2. Missing firmware — check:   sudo dmesg | grep firmware")
        else:
            print(f"  {C.DIM}Unknown chipset{C.RESET}  [{vid_pid}]")
            print(f"       Name:    {dev.name}")
            print(f"       Driver:  {dev.driver or C.DIM + 'unknown' + C.RESET}")
            iface_display = dev.interface or f"{C.DIM}not assigned{C.RESET}"
            print(f"       Iface:   {iface_display}")
            warn("This chipset is not in apsta's database.")
            info("Run:  apsta detect   to check AP+STA via iw list")

        print()

    kernel_ver = run_out("uname -r").split("-")[0]
    _warn_kernel_if_needed(devices, kernel_ver)


def _warn_kernel_if_needed(devices: List[UsbWifiDevice], kernel_ver: str):
    def parse_ver(v: str) -> Tuple[int, ...]:
        try:
            return tuple(int(x) for x in v.split(".")[:2])
        except ValueError:
            return (0, 0)

    running = parse_ver(kernel_ver)
    for dev in devices:
        if dev.chipset_db:
            required = parse_ver(dev.chipset_db.min_kernel)
            if running < required:
                warn(f"{dev.chipset_db.chipset} requires kernel {dev.chipset_db.min_kernel}+, "
                     f"but you're running {kernel_ver}.")
                info("AP mode will not work until you upgrade your kernel.")


def cmd_recommend(args):
    head("apsta — USB Adapter Recommendations")
    print()

    ifaces = get_wifi_interfaces()
    builtin_has_ap_sta = False
    if ifaces:
        target = next((i for i in ifaces if i.state == "UP"), ifaces[0])
        cap = get_hardware_capability(target.name)
        builtin_has_ap_sta = cap.supports_ap_sta_concurrent or cap.supports_ap_sta_split

    if builtin_has_ap_sta:
        ok("Your built-in card already supports AP+STA simultaneously.")
        ok("You don't need a USB dongle.")
        info("Run:  sudo apsta start")
        print()
        return

    usb_devices = scan_usb_wifi()
    capable_plugged = [d for d in usb_devices if d.chipset_db and d.chipset_db.ap_sta]
    if capable_plugged:
        ok("You already have a compatible USB adapter plugged in:")
        for dev in capable_plugged:
            print(f"     {C.BOLD}{dev.chipset_db.chipset}{C.RESET}  [{dev.vid}:{dev.pid}]"
                  f"  iface: {dev.interface or C.DIM + 'not yet assigned' + C.RESET}")
        print()
        info("Configure it:  sudo apsta config --set interface=<iface>")
        info("Then start:    sudo apsta start")
        print()
        return

    warn("Your built-in card does not support concurrent AP+STA.")
    warn("A USB WiFi dongle is needed to run a hotspot without dropping WiFi.")
    print()
    head("Recommended USB Adapters")
    print()

    recommended = [cs for cs in USB_CHIPSET_DB if cs.ap_sta]
    for cs in recommended:
        wifi_color = C.CYAN if "6" in cs.wifi_gen or "7" in cs.wifi_gen else C.DIM
        print(f"  {C.BOLD}{cs.chipset}{C.RESET}  {wifi_color}{cs.wifi_gen}{C.RESET}  "
              f"(kernel {cs.min_kernel}+)")
        print(f"       Driver:  {cs.driver}  (in-kernel, plug and play)")
        print(f"       Search:  {C.YELLOW}{cs.buy_search}{C.RESET}")
        if cs.notes:
            print(f"       Notes:   {C.DIM}{cs.notes}{C.RESET}")
        print()

    info("After plugging in an adapter, run:  apsta scan-usb")
    info("to verify it's detected, then:    sudo apsta config --set interface=<iface>")
    print()

    kernel_ver = run_out("uname -r").split("-")[0]
    info(f"Your kernel: {kernel_ver}")
    print()


