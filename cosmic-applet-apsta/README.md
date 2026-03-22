# cosmic-applet-apsta

COSMIC panel applet for [apsta](../README.md) — the AP+STA WiFi hotspot manager.

Shows a WiFi icon in your panel. Click to toggle the hotspot on/off, change SSID
and password, and run hardware detection — all without a terminal.

## Requirements

- COSMIC DE (Pop!\_OS or any distro with cosmic-session)
- `apsta` installed at `/usr/local/bin/apsta` (Phase 1–3)
- Rust 1.75+
- `just` — `cargo install just`
- System deps: `sudo apt install cmake libexpat1-dev libfontconfig-dev libfreetype-dev libxkbcommon-dev pkgconf`

## Build and Install

```bash
cd cosmic-applet-apsta
just install
```

Then add it to your panel:
**COSMIC Settings → Desktop → Panel → Configure Panel Applets → Add**

## Privilege Escalation

`start` and `stop` need root. The applet uses `pkexec` — the standard
Wayland-compatible privilege escalation tool. You'll see a system auth dialog
when starting or stopping the hotspot.

## Development

```bash
# Build only
just build

# Run directly (must be inside a COSMIC session)
just run
```
