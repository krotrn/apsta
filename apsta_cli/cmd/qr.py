"""qr: a QR code a phone camera can scan to join the hotspot."""

from __future__ import annotations

import io
from typing import Optional

from ..config import store
from ..core import output
from ..core.errors import ApstaError
from ..services import hotspot


def escape_wifi_field(value: str) -> str:
    for ch in ("\\", ";", ",", ":", '"'):
        value = value.replace(ch, "\\" + ch)
    return value


def wifi_share_string(ssid: str, password: str, hidden: bool = False) -> str:
    """The ``WIFI:`` payload phone cameras understand; ``H:true`` for hidden networks."""
    tail = "H:true;" if hidden else ""
    return f"WIFI:T:WPA;S:{escape_wifi_field(ssid)};P:{escape_wifi_field(password)};{tail};"


def render_qr(payload: str) -> Optional[str]:
    """The QR code as terminal text, or None if the qrcode module is missing."""
    try:
        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    buf = io.StringIO()
    # Inverted: dark modules print as spaces, which reads correctly on dark terminals.
    qr.print_ascii(out=buf, invert=True)
    return buf.getvalue()


def cmd_qr(args) -> int:
    hotspot.require_root("Showing the password")
    config = store.load()
    if not config.get("password"):
        raise ApstaError(
            "No password is set yet.",
            hints=["Start the hotspot once to generate one, or set it: sudo apsta config --password-stdin"],
        )
    payload = wifi_share_string(config["ssid"], config["password"], bool(config.get("hidden")))
    output.head("apsta — Join with a phone")
    code = render_qr(payload)
    if code is None:
        output.warn("Install python-qrcode (Arch) or python3-qrcode (Debian/Ubuntu) to show a QR code.")
        output.info(f"QR payload: {payload}")
    else:
        print(code, end="")
        output.info("Point a phone camera at the code to join.")
    output.info(f"Network: {config['ssid']}{' (hidden)' if config.get('hidden') else ''}")
    output.reveal_secret("Password", config["password"])
    output.blank()
    return 0
