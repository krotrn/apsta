"""Shared test helpers: isolated paths, a scriptable fake shell, fixtures."""

from __future__ import annotations

import tempfile
import unittest.mock as mock
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Union

from apsta_cli.core import paths, shell

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def isolate_paths(testcase, root: Optional[Path] = None) -> Path:
    """Point every apsta path into a fresh temp dir for the duration of the test."""
    if root is None:
        tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
    etc, run = root / "etc", root / "run"
    values = {
        "CONFIG_DIR": etc,
        "CONFIG_PATH": etc / "config.json",
        "SECRETS_PATH": etc / "secrets.json",
        "RUN_DIR": run,
        "STATE_PATH": run / "state.json",
        "LOCK_PATH": run / "lock",
        "RESUME_MARKER": run / "resume-pending",
        "HOSTAPD_CONF": run / "hostapd.conf",
        "HOSTAPD_ACCEPT": run / "hostapd.accept",
        "HOSTAPD_CTRL_DIR": run / "hostapd",
        "HOSTAPD_PID": run / "hostapd.pid",
        "DNSMASQ_CONF": run / "dnsmasq.conf",
        "DNSMASQ_PID": run / "dnsmasq.pid",
        "DNSMASQ_LEASES": run / "dnsmasq.leases",
        "WPA_CTRL_DIR": run / "wpa_supplicant",
        "NM_RUNTIME_KEYFILE_DIR": run / "NetworkManager",
        "NM_RUNTIME_CONF_DIR": run / "NetworkManager-conf.d",
        "LOG_PATH": root / "apsta.log",
        "SYSFS_NET": root / "sys" / "class" / "net",
        "IP_FORWARD": root / "ip_forward",
    }
    values["SYSFS_NET"].mkdir(parents=True, exist_ok=True)
    values["IP_FORWARD"].write_text("0\n")
    for name, value in values.items():
        patcher = mock.patch.object(paths, name, value)
        patcher.start()
        testcase.addCleanup(patcher.stop)
    # Never reach the real system bus (a developer's wpa_supplicant) from tests.
    patcher = mock.patch("apsta_cli.net.wpa.dbus_available", return_value=False)
    patcher.start()
    testcase.addCleanup(patcher.stop)
    return root


Response = Union[shell.Result, Callable[[List[str]], shell.Result]]


class FakeShell:
    """Replaces ``shell.run``: answers by argv prefix and records every call."""

    def __init__(self, tools: Sequence[str] = ()):
        self.calls: List[List[str]] = []
        self.inputs: List[Optional[str]] = []
        self._rules: List[tuple] = []
        self.tools = set(tools)

    def on(self, *prefix: str, stdout: str = "", rc: int = 0, stderr: str = "", fn: Optional[Callable] = None):
        self._rules.append((list(prefix), fn, rc, stdout, stderr))
        return self

    def __call__(self, argv, input=None, timeout=None, daemonizes=False):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        self.inputs.append(input)
        for prefix, fn, rc, stdout, stderr in reversed(self._rules):
            if argv[: len(prefix)] == prefix:
                if fn is not None:
                    return fn(argv)
                return shell.Result(argv, rc, stdout, stderr)
        return shell.Result(argv, 0, "", "")

    def install(self, testcase) -> FakeShell:
        for target, value in (
            ("apsta_cli.core.shell.run", self),
            ("apsta_cli.core.shell.which", lambda name: f"/usr/bin/{name}" if name in self.tools else None),
        ):
            patcher = mock.patch(target, value)
            patcher.start()
            testcase.addCleanup(patcher.stop)
        return self

    def called(self, *prefix: str) -> bool:
        return any(c[: len(prefix)] == list(prefix) for c in self.calls)

    def matching(self, *prefix: str) -> List[List[str]]:
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


def as_root(testcase) -> None:
    patcher = mock.patch("os.geteuid", return_value=0)
    patcher.start()
    testcase.addCleanup(patcher.stop)


class FakeWpaSupplicant:
    """A wpa_supplicant control socket on a thread: records commands, answers via ``handler``.

    ``handler(command) -> reply``; the default (:meth:`default`) keeps network
    blocks like wpa_supplicant does, including the copy it stores of a
    persistent group when the group starts. ``networks`` maps id -> settings.
    """

    def __init__(self, path: Path, handler: Optional[Callable[[str], str]] = None):
        import socket
        import threading

        self.path = path
        self.commands: List[str] = []
        self.handler = handler or self.default
        self.networks: dict = {}
        path.parent.mkdir(parents=True, exist_ok=True)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(str(path))
        self.sock.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _new_id(self) -> int:
        return max(self.networks, default=-1) + 1  # what wpa_supplicant does too

    def default(self, command: str) -> str:
        verb, _, rest = command.partition(" ")
        args = rest.split()
        if verb == "ADD_NETWORK":
            net_id = self._new_id()
            self.networks[net_id] = {}
            return f"{net_id}\n"
        if verb == "SET_NETWORK":
            self.networks[int(args[0])][args[1]] = args[2] if len(args) > 2 else ""
        if verb == "P2P_GROUP_ADD":
            # wpa_supplicant records the running group: one copy per name, reused.
            ssid = self.networks[int(args[0].split("=")[1])].get("ssid")
            copies = [i for i, n in self.networks.items() if n.get("copy") and n.get("ssid") == ssid]
            if not copies:
                self.networks[self._new_id()] = {"ssid": ssid, "disabled": "2", "copy": True}
        if verb == "LIST_NETWORKS":
            lines = ["network id / ssid / bssid / flags"]
            for net_id, n in self.networks.items():
                flags = "[DISABLED][P2P-PERSISTENT]" if n.get("disabled") == "2" else ""
                lines.append(f"{net_id}\t{bytes.fromhex(n.get('ssid', '')).decode()}\tany\t{flags}")
            return "\n".join(lines) + "\n"
        if verb == "GET_NETWORK":
            return f'"{bytes.fromhex(self.networks[int(args[0])].get("ssid", "")).decode()}"\n'
        if verb == "REMOVE_NETWORK":
            return "OK\n" if self.networks.pop(int(args[0]), None) is not None else "FAIL\n"
        return "OK\n"

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                continue
            command = data.decode()
            self.commands.append(command)
            try:
                reply = self.handler(command)
            except (KeyError, ValueError, IndexError):
                reply = "FAIL\n"  # e.g. an unknown network id, as wpa_supplicant answers
            if reply is not None:
                self.sock.sendto(reply.encode(), addr)

    def close(self) -> None:
        self._stop.set()
        self._thread.join()
        self.sock.close()

    def install(self, testcase) -> FakeWpaSupplicant:
        testcase.addCleanup(self.close)
        return self


class FakeBus:
    """Stands in for a jeepney connection to wpa_supplicant's D-Bus API.

    ``handler(path, member, body)`` returns ``(signature, body)`` for a reply
    or ``("error", name)`` for a D-Bus error; the default plays a well-behaved
    wpa_supplicant. Every call is recorded as ``(path, member, body)``.
    """

    ROOT = "/fi/w1/wpa_supplicant1"

    def __init__(self, handler: Optional[Callable] = None):
        self.calls: List[tuple] = []
        self.handler = handler or self.default
        self.interfaces: dict = {}
        self.groups: dict = {}  # persistent group path -> ssid as wpa_supplicant reports it ('"name"')
        self._next_group = 0

    def iface_path(self, name: str) -> str:
        return f"{self.ROOT}/Interfaces/{self.interfaces.setdefault(name, len(self.interfaces))}"

    def _add_group(self, iface: str, ssid: str) -> str:
        group = f"{iface}/PersistentGroups/{self._next_group}"
        self._next_group += 1
        self.groups[group] = ssid
        return group

    def default(self, path, member, body):
        if member == "GetInterface":
            return "o", (self.iface_path(body[0]),)
        if member == "AddPersistentGroup":
            return "o", (self._add_group(path, f'"{body[0]["ssid"][1].decode()}"'),)
        if member == "GroupAdd":
            # Like wpa_supplicant: store a copy of the running group, one per name.
            ssid = self.groups[body[0]["persistent_group_object"][1]]
            if list(self.groups.values()).count(ssid) < 2:
                self._add_group(path, ssid)
        if member == "RemovePersistentGroup" and self.groups.pop(body[0], None) is None:
            return "error", "fi.w1.wpa_supplicant1.PersistentGroupUnknown"
        if member == "Get" and body[1] == "PersistentGroups":
            return "v", (("ao", list(self.groups)),)
        if member == "Get" and body[1] == "Properties":
            return "v", (("a{sv}", {"ssid": ("s", self.groups[path]), "mode": ("s", "3")}),)
        return "", ()

    def send_and_get_reply(self, msg, timeout=None):
        from jeepney import new_error, new_method_return
        from jeepney.low_level import HeaderFields

        path, member = msg.header.fields[HeaderFields.path], msg.header.fields[HeaderFields.member]
        self.calls.append((path, member, msg.body))
        signature, body = self.handler(path, member, msg.body)
        if signature == "error":
            return new_error(msg, body, "s", ("refused",))
        return new_method_return(msg, signature or None, body)

    def close(self) -> None:
        pass

    def members(self) -> List[str]:
        return [member for _, member, _ in self.calls]

    def install(self, testcase) -> FakeBus:
        for target, value in (
            ("apsta_cli.net.wpa._connect", lambda: self),
            ("apsta_cli.net.wpa.dbus_available", lambda: True),
        ):
            patcher = mock.patch(target, value)
            patcher.start()
            testcase.addCleanup(patcher.stop)
        return self
