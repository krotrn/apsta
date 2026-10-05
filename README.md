# apsta

**Run a WiFi hotspot on Linux while staying connected to WiFi.**

[![CI](https://github.com/krotrn/apsta/actions/workflows/ci.yml/badge.svg)](https://github.com/krotrn/apsta/actions/workflows/ci.yml)

`nmcli device wifi hotspot` takes over your WiFi card and drops your
connection. Most modern cards can actually be a client and an access point at
the same time ("AP+STA"). apsta finds out whether yours can, sets the hotspot
up the right way for it, keeps it healthy, and cleans up completely when you
stop it.

```
$ apsta detect

Capability report for wlo1
  →  Driver:   iwlwifi
  →  Chipset:  Intel Corporation Wi-Fi 6 AX200

     ✔  AP mode (hotspot)                    yes
     ✔  STA mode (WiFi client)               yes
     ✔  AP + STA at the same time            yes
     ✘  AP on a different channel than STA   no

Methods
     hostapd  ready
     nmcli    ready

Verdict
  ✔  Your card can run a hotspot while staying connected to WiFi.
  ✔  The hotspot will use the same channel as your WiFi connection.
  →  Next: sudo apsta start
```

## Features

- **Reads the hardware properly**: parses the driver's interface combinations
  to decide whether AP+STA is possible and whether the hotspot must share the
  WiFi channel.
- **Keeps your connection**: the hotspot runs on a virtual interface. If
  your WiFi's channel can't host (radar/DFS channels, or 5 GHz channels the
  card blocks), it runs as a Wi-Fi Direct group on a channel of its own, as
  Windows does, on cards that support that. A mode that drops WiFi is used
  only if you allow it (`--allow-disconnect`).
- **All-or-nothing setup**: every step registers its own undo, so a failure
  part-way leaves nothing behind, and `stop` removes exactly what `start` added.
- **Stays up**: a watcher restarts the hotspot after suspend/resume or a
  driver reset, moves it when your WiFi changes channel, and steps aside when
  it would keep your WiFi from reconnecting.
- **Works with your firewall**: firewalld, iptables (including ufw/docker
  setups) and nftables. Picks a hotspot subnet that doesn't clash with your
  networks. DNS goes through your normal resolver, so captive portals and VPN
  DNS keep working.
- **Fast**: 802.11n/ac + WMM, and the right regulatory country code.
- **Client management** (hostapd mode): list, kick, block, and rate-limit
  clients, or let only the devices you list join.
- **Hidden network** option, and a QR code to join (`apsta qr` or the app).
- **Safe by default**: a random password on first start (never a shared
  default), passwords never on a command line, a scoped polkit action for the
  GUI.
- **Desktop app** (GTK 4/libadwaita) with device management and QR-code sharing.

## Install

### Ubuntu / Pop!\_OS (PPA)

```bash
sudo add-apt-repository ppa:krotrn/apsta
sudo apt update
sudo apt install apsta
```

### Arch Linux and derivatives

```bash
sudo tee -a /etc/pacman.conf >/dev/null <<'CONF'

[apsta]
SigLevel = Optional TrustAll
Server = https://github.com/krotrn/apsta/releases/download/arch-repo
CONF
sudo pacman -Sy apsta          # or: yay -S apsta
sudo pacman -S --needed hostapd dnsmasq   # recommended (hostapd mode)
```

### From source (any distribution)

```bash
git clone https://github.com/krotrn/apsta && cd apsta
sudo ./install.sh              # installs into /usr/local
sudo ./install.sh --uninstall  # removes it again
```

Runtime requirements: Python ≥ 3.10, NetworkManager, `iw`, `iproute2`.
Recommended: `hostapd` + `dnsmasq` (needed for client management), and one of
`iptables`/`nftables`/`firewalld`. On Fedora, openSUSE, Alpine and Void, the
Python library `jeepney` enables the [Wi-Fi Direct fallback](docs/wifi-direct.md). Desktop app: see [Desktop app](#desktop-app).

> **pipx/pip users:** `sudo` can't see `~/.local/bin`. Install system-wide
> with `sudo pipx install --global apsta` (pipx ≥ 1.5), or use the packages above.

<a href="https://www.buymeacoffee.com/krotrn" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me a Coffee" style="height: 60px !important;width: 217px !important;" ></a>

## Usage

```bash
apsta detect                         # what can my card do?
sudo apsta start                     # start (keeps WiFi when the card allows it)
sudo apsta start --allow-disconnect  # also allow a mode that drops WiFi
sudo apsta stop
apsta status                         # --check: exit code only (0 running, 3 not)

sudo apsta enable                    # start at boot, recover after sleep (systemd/OpenRC/runit)
sudo apsta disable
```

### Configuration

```bash
apsta config                              # show the active profile
sudo apsta config --set ssid=MyHotspot --set band=a
sudo apsta config --set method=p2p --set channel=149   # own channel, always on 5 GHz 149
sudo apsta config --password-stdin        # prompts; nothing ends up in shell history
sudo apsta config --generate-password
sudo apsta config --show-password
sudo apsta qr                             # QR code a phone camera can scan to join

apsta profile list
sudo apsta profile create travel
sudo apsta profile use travel
```

Settings:

| Setting | Values | Meaning |
| ------- | ------ | ------- |
| `ssid`, `password` | | The network's name and password |
| `band` | `bg` (2.4 GHz), `a` (5 GHz) | Used whenever the hotspot has a channel of its own |
| `channel` | `auto` (default) or a number | `auto` picks the least crowded channel the card allows |
| `method` | `auto` (default), `hostapd`, `nmcli`, `p2p`, `nmcli-single` | How the hotspot runs. `p2p` (Wi-Fi Direct) always uses your band and channel, at the cost of shared speed ([details](docs/wifi-direct.md)) |
| `interface` | `auto` (default) or a name | Which WiFi card to use |
| `hidden` | `yes`, `no` | Stop broadcasting the name; devices type it or scan the QR code |
| `allowed_macs` | MAC list | Only these devices may join (see below) |

On most laptop cards a normal hotspot has to share your WiFi's channel, so
`band` and `channel` can't always be followed. apsta never ignores them
silently: `apsta start`, `apsta status` and the app's *Why it runs this way*
section say what it chose and why, and so does `/var/log/apsta.log`.

Configuration lives in `/etc/apsta/` and passwords in the root-only
`/etc/apsta/secrets.json`.

### Clients (hostapd mode)

```bash
apsta clients                               # connected stations with IP/hostname
sudo apsta clients disconnect phone         # by hostname, IP or MAC
sudo apsta clients disconnect phone --block # and keep it out
sudo apsta clients unblock aa:bb:cc:dd:ee:ff
sudo apsta clients limit 192.168.42.17 8000 # Kbps, upload and download
sudo apsta clients unlimit 192.168.42.17
```

To let only your own devices join, list their MAC addresses. Anyone else is
refused even with the password. This needs hostapd mode; apsta won't start a
hotspot it can't restrict.

```bash
sudo apsta config --set allowed_macs="aa:bb:cc:dd:ee:ff, 11:22:33:44:55:66"
sudo apsta config --set allowed_macs=      # anyone with the password again
```

Phones often use a random MAC per network: look up the one they use for your
hotspot (Android: *Wi-Fi → your hotspot → Privacy*, or set it to the device
MAC; iPhone: *Private Wi-Fi Address*).

### Scripting

`status`, `detect`, `config`, `clients` and `start` accept `--json`. See
[docs/json-output.md](docs/json-output.md) for the keys and exit codes.

```bash
apsta status --json | jq -r '.clients[].ip'
```

## Desktop app

<p>
  <img src="docs/screenshots/hotspot.png" alt="Hotspot tab" width="300">
  <img src="docs/screenshots/devices.png" alt="Devices tab" width="300">
</p>
<p>
  <img src="docs/screenshots/share.png" alt="Share dialog with QR code" width="300">
  <img src="docs/screenshots/narrow.png" alt="Narrow window with tabs at the bottom" width="170">
</p>

Open **Hotspot (apsta)** from your app menu, or run `apsta-gtk`.

- **Hotspot**: one button to start/stop, whether you're still connected to
  your WiFi, connection details, a profile switcher, and **Share**: a QR code
  phones can scan, plus the password.
- **Devices**: everything connected, with a menu per device to limit its
  speed, disconnect it, or block it. Blocked devices can be unblocked here.
- **Settings**: network name, password, band, interface, profiles, *Start
  automatically* (boot + recovery after sleep), and what your WiFi card supports.

The window adapts to its size (tabs move to the bottom on narrow windows),
and follows your light/dark preference. Shortcuts: <kbd>Ctrl</kbd>+<kbd>R</kbd>
or <kbd>F5</kbd> refresh, <kbd>Ctrl</kbd>+<kbd>Q</kbd> quit.

Starting, stopping and changing settings ask for your password through
polkit. It's remembered for a few minutes, so you aren't asked on every click.

**Requirements.** The packages above install everything. From source, install
your distribution's GTK bindings:

| Distribution           | Packages                                                                              |
| ---------------------- | ------------------------------------------------------------------------------------- |
| Ubuntu / Debian / Mint | `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-qrcode python3-pil`                   |
| Fedora                 | `python3-gobject gobject-introspection gtk4 libadwaita python3-qrcode python3-pillow`  |
| Arch                   | `python-gobject gtk4 libadwaita python-qrcode python-pillow`                           |

It works with libadwaita 1.1 / GTK 4.6 and newer, and uses newer libadwaita
features when your system has them. CI checks it on Ubuntu 22.04, Debian 12,
Ubuntu 24.04, Fedora and Arch. `python3-qrcode` is optional; without it,
Share shows the name and password only.

### Shell completion

```bash
apsta completion bash | sudo tee /etc/bash_completion.d/apsta >/dev/null
apsta completion zsh  | sudo tee /usr/local/share/zsh/site-functions/_apsta >/dev/null
apsta completion fish | sudo tee /etc/fish/completions/apsta.fish >/dev/null
```

## How it works

`apsta start` picks the first method that works on your machine:

| Method         | When                                    | WiFi stays up | Client management |
| -------------- | --------------------------------------- | ------------- | ----------------- |
| `hostapd`      | card supports AP+STA, hostapd installed | yes           | yes               |
| `nmcli`        | card supports AP+STA                    | yes           | list/kick         |
| `p2p`          | WiFi's channel can't host; card can run Wi-Fi Direct on a second channel | yes (speed shared) | list/kick |
| `nmcli-single` | card supports AP only                   | **no**        | list/kick         |

```mermaid
flowchart LR
    S(["sudo apsta start"]) --> H{"AP+STA card and<br/>hostapd installed?"}
    H -- "yes" --> HA["hostapd ✔<br/>WiFi stays up"]
    H -- "no / failed" --> N{"AP+STA card?"}
    N -- "yes" --> NM["nmcli ✔<br/>WiFi stays up"]
    N -- "no / failed" --> P{"Wi-Fi Direct on a<br/>second channel?"}
    P -- "yes" --> PG["p2p ✔<br/>WiFi stays up, own channel"]
    P -- "no / failed" --> D{"WiFi not connected, or<br/>--allow-disconnect?"}
    D -- "yes" --> NS["nmcli-single ✔<br/>WiFi drops"]
    D -- "no" --> E(["Explains why, suggests a fix"])
```

A method that fails part-way is rolled back completely before the next one
is tried.

On single-channel cards (most laptop chips) the hotspot has to use the same
channel as your WiFi. apsta reads it from the live connection. Some channels
can't host a hotspot: radar (DFS) channels 52–144, 6 GHz, and 5 GHz channels
the card's firmware blocks. On cards that can run a Wi-Fi Direct group on a
second channel (most Intel cards) apsta then gives the hotspot a channel of
its own; the radio switches between the two, so they share its speed. See
[docs/wifi-direct.md](docs/wifi-direct.md) for which cards and distributions
support it. On other cards apsta refuses with an explanation; see
[docs/5ghz-wifi.md](docs/5ghz-wifi.md).
The hotspot follows the connection when it changes channel (on systemd,
also when started with `apsta start` or the GUI).

New to this? [docs/how-it-works.md](docs/how-it-works.md) explains it in plain
language, with the common problems and fixes. For the design, see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Troubleshooting

- **Won't start while connected to a 5 GHz network** (e.g. a phone hotspot
  or a campus network on a DFS channel)? Your card can't host on that channel
  and has no [Wi-Fi Direct fallback](docs/wifi-direct.md) (`apsta detect`
  shows whether it does, and what's missing).
  Switch the network to 2.4 GHz, or see [docs/5ghz-wifi.md](docs/5ghz-wifi.md)
  for why and the other fixes.
- `APSTA_DEBUG=1 sudo apsta start` prints every step; privileged runs also log
  JSON lines to `/var/log/apsta.log`.
- The service's own log: `journalctl -u apsta`.
- In hostapd mode, `journalctl -u apsta-hostapd -u apsta-dnsmasq` shows the
  daemons' own logs.
- Wrong verdict from `apsta detect`? Please open an issue with
  `apsta detect --json` and `iw phy phy0 info`.

**Desktop app:**

- *"No polkit authentication agent is running"*: your session has no agent to
  ask for the password. GNOME, KDE, Cinnamon, MATE and Xfce start one; on
  i3/sway/Hyprland start one yourself, e.g. `polkit-gnome` or `lxpolkit`.
- *"Not authorized"*: your account isn't an administrator (not in `sudo` /
  `wheel`).
- *"apsta is not installed"* window: the desktop app can't find the `apsta`
  command. Install the CLI (it's in the same package) or put it on `PATH`.
- Settings changes apply to the next start; stop and start the hotspot.

If your card can't do AP+STA, `apsta recommend` suggests USB adapters with
in-kernel drivers that can (MediaTek mt7921au, mt7612u, mt7610u, mt7925u).

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). The test
suite runs without root or WiFi hardware.

Security issues: see [SECURITY.md](SECURITY.md).

## License

MIT
