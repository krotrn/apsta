"""A tiny simulated Linux network stack for integration tests.

Each fake tool (iw, ip, nmcli, hostapd, ...) is a small executable that calls
``main(<tool>)`` here. They share state through ``world.json`` in
``$APSTA_FAKE_ROOT`` and mirror interfaces into the fake sysfs that apsta reads,
so apsta runs its real code paths — subprocesses, files, daemons, pidfiles —
without root or WiFi hardware.
"""

import fcntl
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path

TOOLS = ("iw", "ip", "nmcli", "hostapd", "dnsmasq", "hostapd_cli", "iptables", "tc", "lspci", "lsusb")

ROOT = Path(os.environ.get("APSTA_FAKE_ROOT", "."))
WORLD = ROOT / "world.json"
SYSFS = ROOT / "sys" / "class" / "net"


# ── setup helpers (used by tests) ─────────────────────────────────────────────


def create(root: Path, phy_info: str, link=None, **extra) -> None:
    world = {
        "phy_info": phy_info,
        "ifaces": {"wlo1": {"type": "managed", "addr": "e4:00:00:00:00:01", "link": link}},
        "addrs": ["192.168.1.23/24"],
        "nm": {},
        "iptables": [],
        "stations": [],
        "calls": [],
        "hostapd_fails": False,
        "reg": "IN",
    }
    world.update(extra)
    (root / "world.json").write_text(json.dumps(world))
    sysfs = root / "sys" / "class" / "net" / "wlo1"
    (sysfs / "phy80211").mkdir(parents=True)
    (sysfs / "phy80211" / "name").write_text("phy0\n")
    (sysfs / "operstate").write_text("up\n")


def install_tools(bindir: Path) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    here = Path(__file__).resolve().parent
    for tool in TOOLS:
        exe = bindir / tool
        exe.write_text(
            f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(here)!r})\n"
            f"import fakeworld\nfakeworld.main({tool!r})\n"
        )
        exe.chmod(0o755)


def load(root: Path) -> dict:
    return json.loads((root / "world.json").read_text())


# ── shared state ──────────────────────────────────────────────────────────────


@contextmanager
def world():
    with open(WORLD, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        data = json.load(f)
        yield data
        f.seek(0)
        f.truncate()
        json.dump(data, f)


def add_iface(w, name, iftype):
    w["ifaces"][name] = {"type": iftype, "addr": "02:00:00:00:00:99", "link": None}
    (SYSFS / name).mkdir(parents=True, exist_ok=True)
    (SYSFS / name / "operstate").write_text("up\n")


def del_iface(w, name):
    w["ifaces"].pop(name, None)
    for child in (SYSFS / name).glob("*"):
        child.unlink()
    if (SYSFS / name).exists():
        (SYSFS / name).rmdir()


def fail(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def daemonize(pidfile, on_start):
    """Fork like a real -B daemon: parent writes the pidfile and exits."""
    pid = os.fork()
    if pid:
        Path(pidfile).write_text(f"{pid}\n")
        sys.exit(0)
    os.setsid()
    on_start()
    for fd in (0, 1, 2):
        os.close(fd)
    time.sleep(120)
    sys.exit(0)


# ── tools ─────────────────────────────────────────────────────────────────────


def iw(w, a):
    if a == ["dev"]:
        out = ["phy#0"]
        for name, i in w["ifaces"].items():
            out += [f"\tInterface {name}", f"\t\taddr {i['addr']}", f"\t\ttype {i['type']}"]
        return "\n".join(out)
    if a[:1] == ["phy"] or a == ["list"]:
        return w["phy_info"]
    if a == ["reg", "get"]:
        return f"global\ncountry {w['reg']}: DFS-ETSI\n"
    if a[0] == "dev":
        name, cmd = a[1], a[2:]
        iface = w["ifaces"].get(name)
        if iface is None:
            fail("command failed: No such device (-19)", 237)
        if cmd == ["info"]:
            return f"Interface {name}\n\ttype {iface['type']}\n"
        if cmd == ["link"]:
            link = iface.get("link")
            if not link or iface["type"] != "managed":
                return "Not connected."
            return f"Connected to aa:bb:cc:dd:ee:ff (on {name})\n\tSSID: {link['ssid']}\n\tfreq: {link['freq']}.0\n"
        if cmd[:2] == ["interface", "add"]:
            add_iface(w, cmd[2], "managed")  # becomes AP once hostapd/NM bring it up
            return ""
        if cmd == ["del"]:
            del_iface(w, name)
            return ""
        if cmd == ["station", "dump"]:
            return "\n".join(f"Station {s['mac']} (on {name})" for s in w["stations"])
        if cmd[:2] == ["station", "del"]:
            w["stations"] = [s for s in w["stations"] if s["mac"] != cmd[2]]
            return ""
    fail(f"fake iw: unsupported {a}")


def ip(w, a):
    if a[:4] == ["-4", "-o", "addr", "show"]:
        return "\n".join(f"3: x    inet {cidr} brd 0" for cidr in w["addrs"])
    if a[:3] == ["-4", "route", "show"]:
        return "default via 192.168.1.1 dev wlo1\n192.168.1.0/24 dev wlo1 proto kernel"
    if a[:3] == ["-4", "neigh", "show"]:
        return ""
    if a[:2] == ["addr", "add"]:
        w["addrs"].append(a[2])
        return ""
    if a[:2] in (["addr", "flush"], ["link", "set"]):
        return ""
    fail(f"fake ip: unsupported {a}")


def nmcli(w, a):
    if a[:2] == ["device", "set"]:
        return ""
    if a[:2] == ["connection", "load"]:
        text = Path(a[2]).read_text()
        con_id = re.search(r"^id=(.+)$", text, re.M).group(1)
        w["nm"][con_id] = {"iface": re.search(r"^interface-name=(.+)$", text, re.M).group(1), "active": False}
        return ""
    if a[:1] == ["--wait"]:
        con = w["nm"].get(a[-1])
        if con is None:
            fail("Error: unknown connection", 10)
        con["active"] = True
        w["ifaces"][con["iface"]]["type"] = "AP"
        return "Connection successfully activated"
    if a[:2] == ["connection", "down"]:
        con = w["nm"].get(a[-1])
        if not con or not con["active"]:
            fail("Error: not active", 10)
        con["active"] = False
        if con["iface"] in w["ifaces"]:
            w["ifaces"][con["iface"]]["type"] = "managed"
        return ""
    if a[:2] == ["connection", "delete"]:
        w["nm"].pop(a[-1], None)
        return ""
    if a[:3] == ["-t", "-f", "CHAN,SIGNAL"]:
        return "1:80\n6:30\n11:75\n"
    fail(f"fake nmcli: unsupported {a}")


def hostapd(w, a):
    if w["hostapd_fails"]:
        fail("nl80211: Could not configure driver mode", 1)
    conf = Path(a[-1]).read_text()
    iface = re.search(r"^interface=(.+)$", conf, re.M).group(1)
    w["ifaces"][iface]["type"] = "AP"
    w["hostapd_conf"] = conf
    return ("daemon", a[a.index("-P") + 1])


def dnsmasq(w, a):
    pidfile = next(x.split("=", 1)[1] for x in a if x.startswith("--pid-file="))
    return ("daemon", pidfile)


def hostapd_cli(w, a):
    cmd = a[a.index("-i") + 2 :]
    if cmd == ["all_sta"]:
        return "\n".join(f"{s['mac']}\nflags=[AUTH][ASSOC]" for s in w["stations"])
    if cmd[0] == "deauthenticate":
        w["stations"] = [s for s in w["stations"] if s["mac"] != cmd[1]]
        return "OK"
    if cmd[0] == "deny_acl":
        w.setdefault("denied", []).append(cmd[2]) if cmd[1] == "ADD_MAC" else None
        return "OK"
    fail(f"fake hostapd_cli: unsupported {a}")


def iptables(w, a):
    table, op, chain = a[a.index("-t") + 1], a[3], a[4]
    rule = " ".join([table, chain, *a[6 if op == "-I" else 5 :]])
    if op == "-I":
        w["iptables"].append(rule)
        return ""
    if rule in w["iptables"]:
        w["iptables"].remove(rule)
        return ""
    fail("iptables: Bad rule (does a matching rule exist in that chain?).", 1)


def tc(w, a):
    w.setdefault("tc", []).append(" ".join(a))
    return ""


def main(tool):
    args = sys.argv[1:]
    with world() as w:
        w["calls"].append([tool, *args])
        handler = {"lspci": lambda w, a: "", "lsusb": lambda w, a: ""}.get(tool) or globals()[tool]
        result = handler(w, args)
    if isinstance(result, tuple) and result[0] == "daemon":
        daemonize(result[1], lambda: None)
    if result:
        print(result)
