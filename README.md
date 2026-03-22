# apsta

**AP+STA WiFi hotspot manager for Linux.**

Stay connected to your WiFi network and broadcast a hotspot simultaneously — without the manual `nmcli` / `hostapd` pain, without dropping your connection, and without touching a config file.

```
$ apsta detect

  apsta — Hardware Detection

  → Found 1 WiFi interface(s):
       wlo1  [e4:c7:67:e4:30:ae]  connected to HomeWiFi

  Capability Report
  → Driver:   iwlwifi
  → Chipset:  Intel Wi-Fi 6 AX200

  ✔  AP mode (hotspot)             supported
  ✔  STA mode (WiFi client)        supported
  ✘  AP+STA simultaneous           not supported

  Verdict
  ⚠  Hardware supports AP but NOT concurrent AP+STA.
  →  Run:  apsta recommend   to see which USB dongle to buy
  →  Run:  sudo apsta start --force   to proceed without a dongle
```

---

## The Problem

On Linux, running a hotspot while staying connected to WiFi is harder than it should be:

- `nmcli device wifi hotspot` **kills your existing WiFi connection** — it takes over the interface completely
- Most WiFi cards don't support concurrent AP+STA mode (being a client and access point simultaneously)
- NetworkManager doesn't tell you *why* it failed or *what your options are*
- COSMIC DE has no hotspot UI at all

`apsta` fixes all of this.

---

## How It Works

```
apsta detect
    ↓
Parse iw list → find "valid interface combinations"
    ↓
Does driver expose { AP, managed } in same combination with total >= 2?
    ↓
YES → create virtual AP interface (wlo1_ap) → hotspot on it, WiFi stays on wlo1
NO  → explain options clearly → suggest ethernet / USB dongle / --force
```

Key technical decisions:
- **Channel sync**: reads live STA frequency via `iw dev link` and forces the AP to the same channel — prevents `Device or resource busy` on single-radio cards
- **Band sync**: derives band (`a` or `bg`) from the same frequency — prevents the `band bg channel 36` invalid combination crash
- **DFS channels**: detects regulatory-blocked channels (52–144) and aborts with clear instructions rather than failing silently
- **Virtual interface MAC**: randomises the locally-administered MAC (`02:xx:xx:xx:xx:xx`) and pins it in NetworkManager to prevent re-randomisation races
- **State persistence**: saves `ap_interface`, `base_interface`, and `active_con_name` to `/etc/apsta/config.json` at start so teardown is exact

---

## Install

```bash
git clone https://github.com/yourusername/apsta
cd apsta
sudo ./install.sh
```

**Dependencies** (all default on Ubuntu/Pop!_OS/Fedora/Arch):
`nmcli` · `iw` · `ip` · `lsusb` · `lspci`

**Python 3.8+** required.

---

## CLI Usage

```bash
# Detect hardware capability
apsta detect

# Start hotspot (auto-detects best method)
sudo apsta start

# Start even if AP+STA not supported (drops WiFi)
sudo apsta start --force

# Stop hotspot
sudo apsta stop

# Show current state
apsta status

# Configure SSID and password
apsta config --set ssid=MyHotspot
sudo apsta config --set password=secret123

# Scan plugged-in USB WiFi adapters
apsta scan-usb

# Suggest a USB adapter to buy
apsta recommend

# Auto-start on boot + survive sleep/wake
sudo apsta enable
sudo apsta disable
```

---

## GUIs

### COSMIC Panel Applet (Pop!_OS / COSMIC DE)

Shows a WiFi icon in your panel. Click to toggle hotspot, change SSID/password, run detect.

```bash
cd cosmic-applet-apsta
just install
# COSMIC Settings → Desktop → Panel → Configure Panel Applets → Add → "apsta Hotspot"
```

Requires: Rust 1.75+, `just`, COSMIC session.

### GTK4 / Libadwaita (GNOME, KDE, Xfce, any desktop)

Full three-page GUI: Status, Hardware, Settings. Works on any desktop running GTK4.

```bash
cd gtk-ui
sudo ./install.sh
# Launch: apsta-gtk  or  "apsta Hotspot Manager" in your app menu
```

Requires: `python3-gi`, `gir1.2-gtk-4.0`, `gir1.2-adw-1`
```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
```

---

## Auto-start and Sleep/Wake Persistence

```bash
sudo apsta enable
```

This installs:
- **`/etc/systemd/system/apsta.service`** — starts hotspot after NetworkManager connects on boot (`nm-online -q` pre-condition, not a fragile `sleep 3`)
- **`/usr/lib/systemd/system-sleep/apsta-sleep`** — tears down hotspot before suspend, restores it after resume

Works on **systemd**, **OpenRC**, and **runit**. Non-systemd users get exact manual instructions and pm-utils hook installation if available.

---

## USB Dongle Support

If your built-in card doesn't support AP+STA:

```bash
# See what's plugged in
apsta scan-usb

# See what to buy
apsta recommend
```

Recommended chipsets (in-kernel drivers, plug and play):

| Chipset | WiFi Gen | Driver | Notes |
|---------|----------|--------|-------|
| mt7921au | WiFi 6 | mt7921u | Best overall. Kernel 5.19+ |
| mt7612u | WiFi 5 | mt76x2u | Rock-solid, works everywhere |
| mt7610u | WiFi 5 | mt76x0u | AC600, great for hotspot-only |
| mt7925u | WiFi 7 | mt7925u | Newest. Kernel 6.7+ |

Realtek chipsets are intentionally excluded — out-of-kernel drivers, unreliable AP+STA.

---

## Compatibility

| Distro | CLI | GTK UI | COSMIC Applet |
|--------|-----|--------|---------------|
| Pop!\_OS 22.04 | ✅ | ✅ | ✅ |
| Pop!\_OS COSMIC | ✅ | ✅ | ✅ |
| Ubuntu 22.04 / 24.04 | ✅ | ✅ | — |
| Fedora 39+ | ✅ | ✅ | — |
| Arch Linux | ✅ | ✅ | — |
| Alpine (OpenRC) | ✅ | ✅ | — |
| Artix (runit) | ✅ | ✅ | — |

---

## Why This Exists

Built out of frustration with Pop!\_OS COSMIC's missing hotspot UI and the silent WiFi-disconnection behaviour of `nmcli hotspot`. If you've ever typed:

```bash
nmcli device wifi hotspot ifname wlan0 ssid foo password bar
```

...and watched your SSH session drop — this is for you.

See [CHANGELOG.md](CHANGELOG.md) for the full development history: 5 phases, 28 bugs fixed, every architectural decision documented.

---

## Contributing

PRs welcome. The Python CLI has no dependencies beyond stdlib. The GTK UI requires PyGObject. The COSMIC applet requires Rust + libcosmic.

If you've tested on a distro not in the table, open an issue with your `apsta detect` output.

---

## License

MIT
