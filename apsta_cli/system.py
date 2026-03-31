#!/usr/bin/env python3
"""Service installation, init integration, and dependency checks."""

import shutil
import sys
import tempfile
from pathlib import Path
from typing import Tuple

from .common import C, SCRIPT_DIR, err, head, info, ok, require_root, run, run_out, warn

SLEEP_HOOK_DEST  = Path("/usr/lib/systemd/system-sleep/apsta-sleep")
SERVICE_DEST     = Path("/etc/systemd/system/apsta.service")

EMBEDDED_SLEEP_HOOK = """#!/usr/bin/env bash
# /usr/lib/systemd/system-sleep/apsta-sleep  (systemd)
# /etc/pm/sleep.d/10_apsta                   (pm-utils / OpenRC)

APSTA=\"/usr/local/bin/apsta\"
STATE_FILE=\"/run/apsta-was-active\"
CONFIG=\"/etc/apsta/config.json\"

case \"$1/$2\" in
    pre/* | suspend/* | hibernate/*)
        ACTION=\"before_sleep\"
        ;;
    post/* | resume/* | thaw/*)
        ACTION=\"after_sleep\"
        ;;
    *)
        exit 0
        ;;
esac

case \"$ACTION\" in
    before_sleep)
        if [ -f \"$CONFIG\" ]; then
            AP_IFACE=$(python3 -c "
import json, sys
try:
    c = json.load(open(sys.argv[1]))
    print(c.get('ap_interface') or '')
except: print('')
" \"$CONFIG\" 2>/dev/null)
            if [ -n \"$AP_IFACE\" ]; then
                touch \"$STATE_FILE\"
                \"$APSTA\" stop
            fi
        fi
        ;;

    after_sleep)
        if [ -f \"$STATE_FILE\" ]; then
            rm -f \"$STATE_FILE\"
            nm-online --timeout 15 -x --quiet 2>/dev/null
            \"$APSTA\" start
        fi
        ;;
esac

exit 0
"""

EMBEDDED_SERVICE_UNIT = """[Unit]
Description=apsta - AP+STA WiFi hotspot manager
After=NetworkManager.service network.target
Wants=NetworkManager.service
PartOf=NetworkManager.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=/usr/bin/nm-online -q --timeout 30
ExecStart=/usr/local/bin/apsta start
ExecStop=/usr/local/bin/apsta stop
TimeoutStopSec=5
SuccessExitStatus=0 1

[Install]
WantedBy=multi-user.target
"""


def _write_embedded_system_files() -> Tuple[Path, Path]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="apsta-system-"))
    sleep_hook_src = tmp_dir / "apsta-sleep"
    service_src = tmp_dir / "apsta.service"

    sleep_hook_src.write_text(EMBEDDED_SLEEP_HOOK)
    sleep_hook_src.chmod(0o755)
    service_src.write_text(EMBEDDED_SERVICE_UNIT)
    service_src.chmod(0o644)

    return sleep_hook_src, service_src

# ── Init system detection ──────────────────────────────────────────────────────

def _detect_init() -> str:
    if Path("/run/systemd/private").exists():
        return "systemd"
    if Path("/run/openrc/softlevel").exists():
        return "openrc"
    if Path("/run/runit").exists():
        return "runit"
    pid1 = run_out("readlink -f /proc/1/exe")
    if "systemd" in pid1:
        return "systemd"
    if "openrc" in pid1:
        return "openrc"
    if "runit" in pid1:
        return "runit"
    return "unknown"

# ── CLI ────────────────────────────────────────────────────────────────────────

def cmd_enable(args):
    require_root()
    head("apsta — Enabling auto-start and sleep/wake persistence")
    print()

    binary_dest = Path("/usr/local/bin/apsta")
    cli_package_src = SCRIPT_DIR / "apsta_cli"
    cli_package_dest = Path("/usr/local/bin/apsta_cli")
    launcher_source = SCRIPT_DIR / "apsta.py"
    if not launcher_source.exists():
        launcher_source = Path(sys.argv[0]).resolve()

    if binary_dest.is_symlink():
        warn(f"{binary_dest} is a symlink — overwriting it with a regular file.")
        warn("If apsta was installed via a package manager, use that to update instead.")
        binary_dest.unlink()

    shutil.copy2(launcher_source, binary_dest)
    binary_dest.chmod(0o755)
    ok(f"Binary installed → {binary_dest}")

    if cli_package_src.exists():
        if cli_package_dest.exists():
            shutil.rmtree(cli_package_dest)
        shutil.copytree(cli_package_src, cli_package_dest)
        for py_file in cli_package_dest.rglob("*.py"):
            py_file.chmod(0o644)
        ok("CLI package installed → /usr/local/bin/apsta_cli/")
    else:
        warn(f"CLI package not found, skipping: {cli_package_src}")

    system_dir     = SCRIPT_DIR / "system"
    sleep_hook_src = system_dir / "apsta-sleep"
    service_src    = system_dir / "apsta.service"

    if not system_dir.exists():
        print()
        warn(f"Bundled system/ directory not found at: {system_dir}")
        info("Using embedded default service/hook templates from this apsta package.")
        sleep_hook_src, service_src = _write_embedded_system_files()

    init = _detect_init()
    info(f"Detected init system: {C.BOLD}{init}{C.RESET}")
    print()

    if init != "systemd":
        _enable_non_systemd(init, sleep_hook_src)
        return

    _enable_systemd(sleep_hook_src, service_src)


def _enable_systemd(sleep_hook_src: Path, service_src: Path):
    import shutil

    for src, label in [(sleep_hook_src, "sleep hook"), (service_src, "service unit")]:
        if not src.exists():
            err(f"Bundled {label} not found: {src}")
            sys.exit(1)

    shutil.copy2(sleep_hook_src, SLEEP_HOOK_DEST)
    SLEEP_HOOK_DEST.chmod(0o755)
    ok(f"Sleep hook installed → {SLEEP_HOOK_DEST}")

    shutil.copy2(service_src, SERVICE_DEST)
    ok(f"Service unit installed → {SERVICE_DEST}")

    _run_sys("systemctl daemon-reload",          "Reloading systemd")
    _run_sys("systemctl enable apsta.service",   "Enabling apsta.service")
    _run_sys("systemctl start apsta.service",    "Starting apsta.service now")

    print()
    ok("apsta will now start automatically on boot and resume from sleep.")
    info("Check service status:  systemctl status apsta")
    print()


def _enable_non_systemd(init: str, sleep_hook_src: Path):
    import shutil

    if init == "openrc":
        warn("OpenRC detected — automated service installation is not supported.")
        print()
        info("To auto-start apsta on boot, add to /etc/local.d/apsta.start:")
        print(f"     {C.DIM}#!/bin/sh")
        print(f"     nm-online -q && /usr/local/bin/apsta start{C.RESET}")
        print()
        info("Make it executable:")
        print(f"     {C.DIM}chmod +x /etc/local.d/apsta.start{C.RESET}")

    elif init == "runit":
        warn("runit detected — automated service installation is not supported.")
        print()
        info("To create a runit service:")
        print(f"     {C.DIM}mkdir -p /etc/sv/apsta")
        print(f"     echo '#!/bin/sh' > /etc/sv/apsta/run")
        print(f"     echo 'exec /usr/local/bin/apsta start' >> /etc/sv/apsta/run")
        print(f"     chmod +x /etc/sv/apsta/run")
        print(f"     ln -s /etc/sv/apsta /var/service/{C.RESET}")

    else:
        warn(f"Unknown init system — cannot automate service installation.")
        print()
        info("Manual setup: run the following at startup (after NetworkManager):")
        print(f"     {C.DIM}nm-online -q && /usr/local/bin/apsta start{C.RESET}")

    print()
    hook_installed = False
    if sleep_hook_src.exists():
        pm_sleep_dir = Path("/etc/pm/sleep.d")
        if pm_sleep_dir.exists():
            dest = pm_sleep_dir / "10_apsta"
            shutil.copy2(sleep_hook_src, dest)
            dest.chmod(0o755)
            ok(f"Sleep/wake hook installed → {dest}  (pm-utils)")
            hook_installed = True
        else:
            warn("pm-utils not found (/etc/pm/sleep.d missing).")
            warn("Sleep/wake persistence requires manual setup.")
            info(f"Hook script is at: {sleep_hook_src}")
    print()
    print(f"  {C.BOLD}Summary{C.RESET}")
    if hook_installed:
        ok("Sleep/wake persistence: installed (see above)")
    else:
        warn("Sleep/wake persistence: NOT installed — requires manual setup (see above)")
    warn("Auto-start on boot:    NOT installed — requires manual setup (see above)")
    info("Once configured, test with:  sudo apsta start")
    print()


def cmd_disable(args):
    require_root()
    head("apsta — Disabling auto-start and sleep/wake persistence")
    print()

    init = _detect_init()
    info(f"Detected init system: {C.BOLD}{init}{C.RESET}")
    print()

    if init != "systemd":
        warn(f"Automated disable is only supported on systemd.")
        warn(f"Remove the service/startup entry you created manually for {init}.")
        pm_hook = Path("/etc/pm/sleep.d/10_apsta")
        if pm_hook.exists():
            pm_hook.unlink()
            ok(f"Removed sleep hook: {pm_hook}")
        print()
        return

    r = run("systemctl stop apsta.service")
    ok("Stopped apsta.service") if r.returncode == 0 else info("apsta.service was not running")

    r = run("systemctl disable apsta.service")
    ok("Disabled apsta.service") if r.returncode == 0 else info("apsta.service was not enabled")

    for path, label in [(SERVICE_DEST, "service unit"), (SLEEP_HOOK_DEST, "sleep hook")]:
        if path.exists():
            path.unlink()
            ok(f"Removed {label}: {path}")
        else:
            info(f"{label} not found (already removed?): {path}")

    _run_sys("systemctl daemon-reload", "Reloading systemd")

    print()
    ok("Auto-start and sleep/wake persistence disabled.")
    info("Hotspot is still running if it was active. Stop with:  sudo apsta stop")
    print()


def _run_sys(cmd: str, label: str):
    result = run(cmd)
    if result.returncode == 0:
        ok(label)
    else:
        err(f"{label} failed")
        if result.stderr:
            print(f"     {C.DIM}{result.stderr.strip()}{C.RESET}")
        sys.exit(1)


def _check_dependencies():
    deps = {
        "nmcli": "network-manager",
        "iw":    "iw",
        "ip":    "iproute2",
        "lsusb": "usbutils",
        "lspci": "pciutils",
    }
    missing = []
    for binary, package in deps.items():
        result = run(f"command -v {binary}")
        if result.returncode != 0:
            missing.append((binary, package))

    if missing:
        err("Missing required dependencies:")
        for binary, package in missing:
            print(f"     {C.BOLD}{binary}{C.RESET}  →  sudo apt install {package}")
        sys.exit(1)

