# Changelog

All notable changes to `apsta` are documented here.

---

## [0.2.0] — Phase 2 Complete

### Summary
Persistent hotspot across reboots and sleep/wake cycles. Systemd service,
unified sleep hook (systemd + pm-utils), init system detection, and
self-installation. Zero known bugs. Tested logic paths across systemd,
OpenRC, and runit environments.

### Added
- `apsta enable` — installs auto-start service and sleep/wake hook; self-copies
  binary to `/usr/local/bin/apsta` so the service survives repo deletion
- `apsta disable` — cleanly removes service unit, sleep hook, and reloads systemd
- `system/apsta.service` — systemd unit with `nm-online -q` pre-condition,
  `TimeoutStopSec=5`, and `SuccessExitStatus=0 1`
- `system/apsta-sleep` — unified sleep hook for both systemd and pm-utils
- `_detect_init()` — detects systemd / OpenRC / runit via `/run/` dirs and
  `/proc/1/exe` fallback; never relies on presence of `systemctl` binary
- Config moved to `/etc/apsta/config.json` — eliminates `$HOME` split-brain
  between user sessions and root-owned systemd/sleep processes
- `save_config` creates `/etc/apsta/` as `755`, file as `644` — non-root
  users can read config (for `apsta status`) but not write it

### Fixed (iteration history)

#### Boot race: `sleep 3` replaced with `nm-online -q`
`ExecStartPre=/bin/sleep 3` was a guess. If NetworkManager took longer than
3 seconds to associate, `_get_sta_channel_band()` returned `None`, the hotspot
started on fallback channel 11, then NM tried to join a 5 GHz router and
crashed with EBUSY. Replaced with `ExecStartPre=/usr/bin/nm-online -q` which
blocks until NM has an actual live connection.

#### Shutdown hang: `TimeoutStopSec=5` added
Without it, if NM's daemon was already dead during system shutdown, `nmcli
connection down` blocked until systemd's default 90-second kill timeout fired.
`TimeoutStopSec=5` caps teardown at 5 seconds.

#### Split-brain config: `Path.home()` → `/etc/apsta/`
`Path.home()` and `$HOME` evaluate to `/root` under sudo and systemd, not
`/home/user`. The service and sleep hook silently ignored user-written config
and used defaults (including the default password). Moving to `/etc/apsta/`
gives all processes one canonical path.

#### `apsta config --set` PermissionError without sudo
After moving config to `/etc/apsta/`, writing required root. `require_root()`
added inside the `if args.set:` branch only — reading config still works
unprivileged.

#### `cmd_disable` aborted before file cleanup
`_run_sys("systemctl stop ...")` called `sys.exit(1)` if the service was
already stopped or the unit was partially deleted, preventing the file deletion
steps below from running. Changed to bare `run()` with per-result logging.

#### pm-utils argument mismatch on non-systemd
`apsta-sleep` used `case "$1/$2"` matching only `pre/*` and `post/*` — the
systemd convention. pm-utils passes a single argument (`suspend`, `resume`,
`hibernate`, `thaw`), making `$1/$2` evaluate to `suspend/` which never
matched. Fixed by adding a normalisation stage: first `case` maps both
conventions to `ACTION=before_sleep` or `ACTION=after_sleep`; second `case`
contains the shared logic. Unknown argument combinations exit cleanly with 0.

#### Non-systemd enable output didn't clearly show incomplete state
On OpenRC/runit, `apsta enable` installed the pm-utils sleep hook but printed
no clear indication that auto-start on boot still needed manual setup. Added
an explicit summary block separating what was installed from what still needs
user action.

#### `apsta enable` broken if repo deleted after install
The systemd service hardcodes `/usr/local/bin/apsta`. If a user ran
`sudo python3 ~/Downloads/apsta.py enable` and then deleted `~/Downloads/`,
the service would break on next boot. `cmd_enable` now copies `sys.argv[0]`
to `/usr/local/bin/apsta` before installing the service unit.

#### Config corrupted by power loss during write
`json.load()` would raise `json.JSONDecodeError` on a truncated file, crashing
the service on next boot. Wrapped in `try/except json.JSONDecodeError` with a
warning and graceful fallback to defaults.

---

## [0.1.0] — Phase 1 Complete

### Summary
First working release. Hardware detection, hotspot lifecycle management, and
AP+STA concurrent mode — all fully functional with robust edge case handling.

### Added
- `apsta detect` — parses `iw list` to report AP, STA, and AP+STA concurrent
  support with driver and chipset identification
- `apsta start` — starts hotspot using best available method; creates virtual
  AP interface if hardware supports concurrent mode
- `apsta stop` — tears down hotspot and cleans up virtual interface
- `apsta status` — shows all active WiFi connections and interface states
- `apsta config` — view and edit persistent JSON configuration
- Pre-flight dependency check for `nmcli`, `iw`, `ip`
- Python 3.8 compatibility (`List`, `Tuple` from `typing`)

### Fixed (iteration history)

#### Combo parser always returned false
`iw list` outputs combination lines starting with `* #{...}`. After `.strip()`,
lines start with `* #{`, not `#{`. Filter changed to check for `"managed"` or
`"AP"` in the line instead of `startswith("#")`.

#### Virtual interface state leak
`cmd_start` created the virtual interface but never saved it to config.
`cmd_stop` always got `None` from `config.get("ap_interface")` and skipped
cleanup. Every `start` call orphaned a `wlo1_ap` interface. Fixed by
persisting `ap_interface`, `base_interface`, and `active_con_name` at start
and clearing at stop.

#### Hardcoded `"Hotspot"` connection name broke teardown
If the SSID was multi-word (e.g. "My Hotspot"), NM named the profile after
the SSID — `nmcli connection down 'Hotspot'` silently failed. Fixed by saving
the actual NM profile name via `_get_active_hotspot_con_name()` at start.

#### 5 GHz channel + `band=bg` mismatch
Passing `band bg channel 36` to nmcli is always invalid. Refactored
`_get_current_channel()` into `_get_sta_channel_band()` returning `(channel,
band)` from the same frequency so they are always consistent.

#### Single-radio EBUSY
Cards with `#channels <= 1` require AP and STA on the exact same channel.
Hardcoded `channel: "11"` crashed any STA on a different channel. Fixed by
reading live frequency from `iw dev <iface> link` at start time.

#### DFS channels kernel-blocked for AP
Falling back to a non-DFS channel on a single-radio card causes EBUSY.
Script now aborts with exact guidance: connect to 2.4 GHz or UNII-1 5 GHz.

#### `config.get(key, default)` passed `None` to nmcli
Keys stored as JSON `null` pass through `.get(key, default)` as `None`. Fixed
with `config.get(key) or default` throughout `cmd_start`.

#### Duplicate MAC rejection on virtual interface
Kernel assigns `wlo1_ap` the same MAC as `wlo1`. Fixed by forcing interface
DOWN, applying a randomised locally-administered MAC (`02:xx:xx:xx:xx:xx`),
then bringing it UP. NM MAC override added to prevent re-randomisation.

#### RTNETLINK EBUSY during MAC change
Kernel sometimes inherits UP state on virtual interface creation. MAC changes
on UP interfaces fail. Fixed by explicit DOWN before MAC assignment.

#### NM global MAC randomisation race
NM could re-randomise `wlo1_ap` immediately after bring-up. Fixed by pinning
the chosen MAC via `nmcli device set {ap_iface} wifi.cloned-mac-address`.

#### Silent fallback to wrong interface
If `interface=wlan1` was configured but the USB dongle was unplugged, the
script silently hijacked `wlo1`. Now aborts with instructions to reset.

#### `time.sleep(1)` unreliable on slow hardware
Replaced with 3-attempt polling loop in `_get_active_hotspot_con_name`.

#### Python 3.9+ type hints crashed on Ubuntu 20.04
`tuple[...]` and `list[...]` require Python 3.9+. Changed to `Tuple[...]`
and `List[...]` from `typing`. Verified with AST scan.

---

## Roadmap

- [x] Phase 1 — Hardware detection + hotspot lifecycle CLI
- [x] Phase 2 — systemd service, sleep/wake persistence, init detection
- [ ] Phase 3 — Multi-distro testing matrix, USB dongle auto-detection
- [ ] Phase 4 — COSMIC DE settings panel (libcosmic / Rust)
- [ ] Phase 5 — GTK4 UI for GNOME / KDE users

---

## [0.4.0] — Phase 4 Complete

### Summary
COSMIC DE panel applet in Rust. Shows hotspot status icon in the panel,
popup with start/stop toggle, SSID/password fields, live status, and
hardware detect output. Background polling keeps icon in sync with daemon.

### Added
- `cosmic-applet-apsta/` — standalone Rust crate, ships as a COSMIC panel applet
- `cosmic::Application` trait implementation with panel icon + popup window
- `subscription()` polling `async_get_status()` every 5 seconds — panel icon
  stays accurate when hotspot is toggled via terminal while popup is closed
- `pkexec` privilege escalation for start/stop — shows system auth dialog,
  no terminal required
- `pkexec_result()` helper maps exit code 126 to "Authentication cancelled."
  instead of the raw multi-line pkexec stderr string
- Status read from `/etc/apsta/config.json` directly via `serde_json` — no
  ANSI parsing, no output format dependency
- `justfile` with `build`, `build-release`, `install`, `uninstall`, `run`
  targets matching COSMIC project conventions
- `data/com.github.apsta.Applet.desktop` — registers with COSMIC panel

### Fixed (iteration history)

#### Triple Polkit prompt on hotspot start
`async_start_hotspot` originally called `run_apsta_sudo` three times
sequentially (set SSID, set password, start). GUI Polkit agents prompt on
every `pkexec` invocation — the user would type their password three times.
Fixed by batching all three into one `pkexec sh -c "...script..." -- "$1" "$2"`
call. SSID and password passed as positional args, not interpolated into the
script string, preventing shell injection.

#### `justfile install` failed on fresh clone
`install` target ran `install -Dm755 target/release/...` without first
building. Added `build-release` as a prerequisite: `install: build-release`.

#### `strip_ansi` consumed valid text after bare ESC
Original parser consumed characters after any `\x1b` until hitting `m`,
regardless of whether `[` followed the ESC. A bare escape character before
text containing `m` would silently delete content. Fixed by requiring the
exact `\x1b[` prefix before entering the skip loop.

#### No background state sync
Panel icon only updated when user clicked it or pressed a button. If the
hotspot was stopped via terminal, the icon stayed green indefinitely. Fixed
with `subscription()` returning `iced::time::every(5s)` mapped to
`Message::RefreshStatus`, which dispatches a silent `async_get_status()` poll
that doesn't interfere with in-progress user actions.
