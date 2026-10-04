# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

Rewrite of the internals around a layered architecture
(see `docs/ARCHITECTURE.md`), fixing a series of correctness, reliability and
security problems.

### Upgrade notes

- **Stop a running hotspot before upgrading from 0.6** (`sudo apsta stop`).
  Runtime state moved from `/etc/apsta/config.json` to `/run/apsta`. The .deb
  and Arch packages do this for you.
- `--force` is now an alias of `--allow-disconnect`. It no longer skips the
  hostapd method; it only *allows* falling back to a mode that drops WiFi.
- `status --disconnect/--limit-client/--use-profile` still work but are
  deprecated. Use `apsta clients …` and `apsta profile use`.
- `detect --json`: `supports_ap_sta_concurrent`/`supports_ap_sta_split` are
  replaced by `ap_sta` and `same_channel_required`.
- Python ≥ 3.9 is required.

### Fixed

- AP+STA detection now applies per-group limits. Cards whose AP and managed
  types share a group limited to one interface are no longer reported as
  concurrent, and multi-channel cards are no longer reported as unsupported.
- hostapd config sets the regulatory `country_code` (5 GHz no longer fails on
  the world domain) and enables 802.11n/ac + WMM (no more 54 Mbps cap).
- 6 GHz WiFi connections are detected; iw ≥ 6 frequency output (`5180.0`) is parsed.
- Start is all-or-nothing: partial failures roll back, and dnsmasq, IP and
  NAT failures are errors, not warnings.
- Starting twice no longer orphans hostapd/dnsmasq; stale state after a crash
  is cleaned up automatically.
- Hotspot subnet is chosen to avoid existing networks. DNS uses the system
  resolver instead of hard-coded 8.8.8.8.
- NAT works with firewalld, nftables, ufw and docker, and through any uplink
  (ethernet, VPN), not only the WiFi interface. `ip_forward` is restored on stop.
- Client list shows associated stations instead of stale DHCP leases.
  Disconnect works (hostapd control socket); clients can be blocked.
- Per-client bandwidth limits no longer overwrite each other. Limits can be removed.
- The resume hook waits for WiFi to reassociate before choosing a channel.
- config.json is written atomically. A corrupt file is backed up instead of
  silently replaced by defaults.
- GUI no longer overwrites fields while you type.

### Security

- No shared default password: a random one is generated on first start.
- Passwords are no longer printed to the journal or passed on command lines
  (nmcli, pkexec).
- GUI uses `pkexec apsta` with a dedicated polkit action instead of `pkexec sh -c`.
- Runtime files moved from `/tmp` to `/run/apsta`; no `shell=True` anywhere.

### Added

- `apsta run`: foreground supervisor used by the service. It restarts the
  hotspot after resume or a driver reset and follows WiFi channel changes.
- hostapd/dnsmasq run as supervised transient systemd units, with logs in the journal.
- `apsta clients` (list/disconnect/`--block`/unblock/limit/unlimit).
- `apsta start --method`, `--wait-sta`, `--ssid`, `--password-stdin`, `--interface`.
- `apsta config --password-stdin`, `--generate-password`, `--show-password`, repeatable `--set`.
- `apsta status --check` (exit code only).
- `apsta enable` supports OpenRC and runit.
- Shell completions generated from the CLI definition.
- Unit and integration test suites (no root or hardware needed), coverage gate in CI.

### Removed

- `setup.py` (pyproject.toml only), `gtk-ui/` launcher scripts (entry points
  and `install.sh` replace them).

## [0.6.2] and earlier

See the git history.
