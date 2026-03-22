# Changelog

All notable changes to `apsta` are documented here.

---

## [0.5.0] — Phase 5 Complete

### Summary
GTK4 / Libadwaita GUI (`apsta-gtk`) for non-COSMIC desktops. Three-page interface:
Status (start/stop with force toggle), Hardware (detect/scan-usb/recommend),
Settings (config + service management). Proper error display, background polling,
and hostapd-aware start behaviour.

### Added
- `apsta-gtk` — GTK4/Libadwaita GUI, works on GNOME, KDE, Xfce, and any GTK4 desktop
- Three-tab layout: Status, Hardware, Settings via `Adw.ViewStack` + `Adw.ViewSwitcher`
- Force start toggle (`Adw.ActionRow` + `Gtk.Switch`) — passes `--force` to apsta
- `Adw.Banner` feedback with 4-second auto-hide and timer cancellation on rapid actions
- Background poll every 5 seconds via `GLib.timeout_add_seconds` with overlap guard
- `gtk-ui/install.sh` — GTK4/Adw dependency check, install to `/usr/local/bin/apsta-gtk`
- `gtk-ui/com.github.apsta.Gtk.desktop` — app menu registration

### Fixed (iteration history)

#### `install.sh` dep check always failed
`for pkg in python3 gi: python3 -c "import $pkg"` — `import python3` is not
valid Python. Replaced with a single `python3 -c` block that does
`gi.require_version` + `from gi.repository import Gtk, Adw` — the exact import
chain the app uses.

#### "Unknown error" on Start Hotspot
`pkexec_error_message(rc, stderr)` returned "Unknown error" when stderr was
empty. apsta writes all output to stdout (coloured terminal output), not stderr.
Fixed by adding `stdout` as a fallback parameter throughout. `first_error_line()`
helper added to extract the first `✘`/`⚠`/`Error` line from multi-line output,
skipping header lines containing `—`.

#### Start Hotspot always failed without Force toggle
`_bg_start` called `apsta start` without `--force`. On single-radio cards
without hostapd, this hits the AP+STA concurrent check and exits non-zero.
Added `force` parameter and `--force` flag passthrough. After Phase 4, `apsta
start` auto-selects hostapd mode so `--force` is no longer needed for Intel
AX200 — but the toggle remains for cards that truly require it.

#### Poll timer race: `_refreshing` cleared before refresh completed
`_poll_worker` called `GLib.idle_add(self._refresh_status)` then immediately
set `self._refreshing = False`. `GLib.idle_add` only schedules the call —
it returns immediately. A second poll tick could fire before the first refresh
finished. Fixed with `threading.Event`: `idle_add` sets the event after
`_refresh_status` returns, `_poll_worker` waits on it before clearing the flag.

#### Banner timer leak on rapid start→stop
Calling `_show_banner` twice within 4 seconds left a stale `GLib.timeout_add`
timer that prematurely hid the newer banner. Fixed by tracking
`_banner_timeout_id` and calling `GLib.source_remove` before creating a new
timer.

---

## [0.4.0] — Phase 4 Complete

### Summary
hostapd-based AP+STA fallback for cards where nmcli concurrent mode fails.
Correctly detects Intel AX200 / iwlwifi split-block interface combinations.
Three-strategy start sequence with method-aware stop. Connected client display
in `apsta status`. COSMIC applet updated with `ssid_edited` guard and
`Vec<String>` fix in `trim_to_verdict`.

### Added
- `supports_ap_sta_split` field in `HardwareCapability` — detects AP+managed
  in separate `#{ }` blocks with `#channels <= 1` (Intel AX200 / iwlwifi case)
- `_start_hostapd_ap_sta()` — creates virtual interface, runs hostapd + dnsmasq,
  assigns `192.168.42.1/24`, sets up NAT via iptables MASQUERADE
- `_stop_hostapd_ap_sta()` — kills hostapd/dnsmasq by PID, removes iptables
  rules, deletes virtual interface
- `_write_hostapd_conf()` / `_write_dnsmasq_conf()` — runtime config generation
- `start_method` key in config — `"nmcli"`, `"hostapd"`, or `"nmcli-force"`;
  `cmd_stop` is now method-aware
- `apsta status` shows connected clients from dnsmasq leases file in hostapd mode
- `_check_hostapd_deps()` — checks for hostapd and dnsmasq before attempting
  hostapd mode, with clear install instruction on failure
- COSMIC applet: `ssid_edited: bool` field — prevents pre-fill from overwriting
  user-typed SSID on status refresh
- COSMIC applet: `#[allow(dead_code)]` on `run_apsta_sudo` — suppresses compiler
  warning for the utility function kept for future use

### Fixed (iteration history)

#### Split-block combinations never detected
`iw list` outputs multi-line combination entries:
```
* #{ managed } <= 1, #{ AP, P2P-client, P2P-GO } <= 1, #{ P2P-device } <= 1,
  total <= 3, #channels <= 1
```
The parser only processed lines starting with `* #{` and checked for `total <=`
on the same line. The second line (with `total` and `#channels`) was always
skipped. Fixed by joining continuation lines: lines not starting with `* #{`
are appended to the previous entry before parsing.

#### `trim_to_verdict` type mismatch (COSMIC applet)
`lines: Vec<&str>` then `lines.push(clean)` where `clean: String` — type
mismatch, would not compile. Changed to `Vec<String>`.

#### `trim_to_verdict` pushed raw ANSI line instead of stripped version
`let clean = strip_ansi(line); lines.push(line)` — `clean` was computed but
the original `line` (with ANSI codes) was pushed. Fixed to `lines.push(clean)`.

#### hostapd mode: `ip link set ap0 up` returned EBUSY
The physical radio is in use by `wlo1`. Bringing up the virtual interface
explicitly fails with EBUSY. hostapd brings the interface up itself during
initialization — the explicit `ip link set up` call removed.

#### `_get_active_hotspot_con_name` polled with sleep-before-check
Loop always slept 1 second before the first check. On fast hardware the profile
was already registered. Fixed to check first, then sleep: `if attempt < 2:
time.sleep(1)`.

#### False "Still connected" message after --force start
`target.connected_ssid` was read before nmcli ran. After nmcli replaced the
interface with an AP, the stored SSID was stale. Fixed: if `ap_iface ==
target.name`, the interface was repurposed as AP — warn about disconnect
instead of claiming still connected.

---

## [0.3.0] — Phase 3 Complete

### Summary
USB WiFi adapter detection and purchase recommendations. Sysfs-first scan
solves duplicate-adapter problem. Chipset database covering mt7921au, mt7925u,
mt7612u, mt7610u. Integration with `apsta detect` verdict.

### Added
- `apsta scan-usb` — walks `/sys/bus/usb/devices/` to find WiFi adapters,
  matches against chipset DB, reports AP+STA capability, kernel version check
- `apsta recommend` — checks built-in card first, then plugged dongles, then
  shows purchase recommendations from DB
- `USB_CHIPSET_DB` — MediaTek chipsets with confirmed in-kernel AP+STA support;
  Realtek intentionally excluded
- `scan_usb_wifi()` — sysfs-first iteration keyed by physical port path, not
  VID:PID — correctly handles two identical adapters on different ports
- `_find_usb_iface_by_path()` — finds kernel interface name and driver per
  physical sysfs entry
- `_warn_kernel_if_needed()` — compares running kernel against `min_kernel`
  per detected adapter

### Fixed (iteration history)

#### Duplicate adapters assigned same interface
lsusb-first scan: two identical mt7921au adapters produce two identical lsusb
lines. VID:PID lookup always returned the first sysfs match for both. Fixed by
iterating sysfs entries first (one entry per physical port) and using lsusb
only for the human-readable name, keyed by Bus+Device number.

#### `_find_usb_iface_by_path` iterated non-directory sysfs entries
`for subdir in dev_path.iterdir()` included files. `subdir / "net"` succeeds
on Path objects regardless of whether `subdir` is a directory. Added
`if not subdir.is_dir(): continue` guard.

---

## [0.2.0] — Phase 2 Complete

### Summary
Persistent hotspot across reboots and sleep/wake cycles. Systemd service,
unified sleep hook (systemd + pm-utils), init system detection, and
self-installation. Zero known bugs.

### Added
- `apsta enable` — installs auto-start service and sleep/wake hook; self-copies
  binary to `/usr/local/bin/apsta`
- `apsta disable` — cleanly removes service unit, sleep hook, reloads systemd
- `system/apsta.service` — systemd unit with `nm-online -q` pre-condition,
  `TimeoutStopSec=5`, `SuccessExitStatus=0 1`
- `system/apsta-sleep` — unified sleep hook for systemd and pm-utils
- `_detect_init()` — detects systemd / OpenRC / runit via `/run/` dirs and
  `/proc/1/exe` fallback
- Config moved to `/etc/apsta/config.json` — eliminates `$HOME` split-brain
- `save_config` creates `/etc/apsta/` as `755`, file as `644`

### Fixed (iteration history)

#### Boot race: `sleep 3` → `nm-online -q`
`ExecStartPre=/bin/sleep 3` failed if NM took longer to associate.
`_get_sta_channel_band()` returned `None`, hotspot started on ch11, NM joined
5 GHz router, EBUSY. Replaced with `ExecStartPre=/usr/bin/nm-online -q`.

#### Shutdown hang: `TimeoutStopSec=5` added
Without it, if NM was dead during shutdown, `nmcli connection down` blocked
for 90 seconds (systemd default kill timeout).

#### Split-brain config: `Path.home()` → `/etc/apsta/`
`Path.home()` evaluates to `/root` under sudo and systemd. Service silently
ignored user config.

#### `apsta config --set` PermissionError without sudo
`require_root()` added inside `if args.set:` branch only.

#### `cmd_disable` aborted before file cleanup
`_run_sys("systemctl stop ...")` called `sys.exit(1)` on non-zero, preventing
file deletion. Changed to bare `run()` with per-result logging.

#### pm-utils argument mismatch
`case "$1/$2"` never matched pm-utils single-argument calling convention.
Added normalisation stage mapping both conventions to `ACTION` variable.

#### `apsta enable` broken if repo deleted post-install
Service hardcodes `/usr/local/bin/apsta`. `cmd_enable` now copies `sys.argv[0]`
to `/usr/local/bin/apsta` before installing the service.

#### Config corrupted by power loss
`json.load()` raised `JSONDecodeError` on truncated file, crashing the service.
Added try/except with warning and fallback to defaults.

#### `NetworkManager-wait-online.service` dependency removed
Ubuntu and some distros mask this unit by default, causing boot hangs.
Replaced with `After=NetworkManager.service` + `ExecStartPre=nm-online -q`.

---

## [0.1.0] — Phase 1 Complete

### Summary
First working release. Hardware detection, hotspot lifecycle management, and
AP+STA concurrent mode.

### Added
- `apsta detect` — parses `iw list` for AP, STA, AP+STA concurrent support
- `apsta start` — starts hotspot using best available method
- `apsta stop` — tears down hotspot and cleans up virtual interface
- `apsta status` — shows active WiFi connections and interface states
- `apsta config` — view and edit persistent JSON configuration

### Fixed (iteration history)

#### Combo parser always returned false
Lines after `.strip()` start with `* #{`, not `#{`. Filter changed to check
for `"managed"` or `"AP"` in the line.

#### Virtual interface state leak
`cmd_stop` always got `None` from `config.get("ap_interface")` — interface
never saved to config at start. Added persistence of `ap_interface`,
`base_interface`, `active_con_name`.

#### Hardcoded `"Hotspot"` connection name
Multi-word SSIDs caused `nmcli connection down 'Hotspot'` to silently fail.
Fixed with `_get_active_hotspot_con_name()`.

#### 5 GHz channel + `band=bg` mismatch
`band bg channel 36` always invalid. `_get_sta_channel_band()` returns
channel and band from the same frequency.

#### Single-radio EBUSY
Hardcoded `channel: "11"` crashed STA on different channel. Now reads live
frequency at start time.

#### DFS channels kernel-blocked for AP
Aborts with guidance: connect to 2.4 GHz or UNII-1 5 GHz first.

#### `config.get(key, default)` passed `None` to nmcli
Keys stored as JSON `null`. Fixed with `config.get(key) or default`.

#### Duplicate MAC rejection on virtual interface
Fixed: force DOWN, apply randomised LA MAC, bring UP, pin in NM.

#### RTNETLINK EBUSY during MAC change
Explicit DOWN added before MAC assignment.

#### NM global MAC randomisation race
Fixed by pinning chosen MAC via `nmcli device set wifi.cloned-mac-address`.

#### Silent fallback to wrong interface
If configured interface missing, now aborts with instructions.

#### `time.sleep(1)` unreliable on slow hardware
Replaced with 3-attempt polling loop in `_get_active_hotspot_con_name`.

#### Python 3.9+ type hints on Ubuntu 20.04
`tuple[...]` → `Tuple[...]`, `list[...]` → `List[...]` from `typing`.