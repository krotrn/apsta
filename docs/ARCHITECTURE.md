# Architecture

This document describes how apsta is put together and why. Read it before a
non-trivial change.

## Layers

```
            ┌──────────────────────┐     ┌────────────────────────────┐
            │ apsta_cli/cli.py     │     │ apsta_gui (GTK4)           │
            │ argparse + errors    │     │ talks to the CLI only      │
            └─────────┬────────────┘     │ (backend.py, pkexec+JSON)  │
                      │                  └────────────┬───────────────┘
            ┌─────────▼────────────┐                  │
 present    │ cmd/*                │◄─────────────────┘
            │ thin: print results  │
            └─────────┬────────────┘
            ┌─────────▼────────────┐
 use cases  │ services/hotspot.py  │ start / stop / status, strategy selection
            │ services/watch.py    │ `apsta run`: keep the hotspot healthy
            └─────────┬────────────┘
       ┌──────────────┼──────────────────────────┐
┌──────▼──────┐ ┌─────▼───────────────────┐ ┌────▼─────────────┐
│ config/     │ │ net/                    │ │ hw/              │
│ model/store │ │ strategies, transaction │ │ combinations     │
│ validate    │ │ hostapd, dnsmasq, nm    │ │ capability       │
│ state.py    │ │ firewall, supervisor    │ │ interfaces, usb  │
└──────┬──────┘ │ iface, subnet, channels │ └────┬─────────────┘
       │        │ clients                 │      │
       │        └─────┬───────────────────┘      │
┌──────▼──────────────▼──────────────────────────▼─────┐
│ core/: shell (argv only), paths, fsutil (atomic),     │
│        lock, output, log, errors                      │
└───────────────────────────────────────────────────────┘
```

Dependencies only point downwards. `core` imports nothing from apsta.
`hw`, `net` and `config` don't print user-facing guidance; they raise
`ApstaError` subclasses that carry `hints`, and `cli.py` renders them and maps
them to exit codes.

## Key decisions

### Hardware capability comes from interface combinations

`hw/combinations.py` parses `iw phy <phy> info` into `Combination(groups,
total, channels)` and asks one question: can one `AP` and one `managed`
interface exist at the same time? That means placing each requested
interface into a group with spare capacity, within `total`. `#channels <= 1`
means the AP must share the STA's channel. Earlier versions guessed from
whether AP and managed appeared in the same `#{}` group, which was wrong in
both directions. Real driver outputs live in `tests/fixtures/iw/`.

### Strategies + transactions

`net/strategies.py` holds three implementations of one interface
(`unavailable`, `start(ctx, tx)`, `stop(state)`): `hostapd`, `nmcli`
(virtual interface) and `nmcli-single`. `services/hotspot.start` tries them
in order. Each `start` registers an undo step with the `Transaction` after
every side effect, so a failure part-way rolls back to a clean system before
the next strategy is tried.

### Runtime state lives in /run

`state.py` writes `HotspotState` to `/run/apsta/state.json` once the hotspot
is fully up, with everything `stop` needs: interface names, subnet, firewall
backend and its undo data, the previous `ip_forward` value, client limits.
`/run` is a tmpfs, so a crash or reboot can't leave a stale "running" record.
On start, a recorded hotspot that is no longer alive is cleaned up first.
Configuration (`/etc/apsta/config.json`, world-readable) and secrets
(`/etc/apsta/secrets.json`, 0600) are separate and written atomically.

### Processes are supervised

`net/supervisor.py` runs hostapd and dnsmasq as transient systemd units
(`apsta-hostapd.service`, `apsta-dnsmasq.service`, `Restart=on-failure`,
logs in the journal). On non-systemd systems it falls back to pidfiles, and
checks a pid against `/proc/<pid>/cmdline` before signalling it.

### The service watches, not just starts

`apsta.service` runs `apsta run --wait-sta 30`. `services/watch.py` polls
every 5 s and `decide()` (a pure function) restarts the hotspot when:

- it went down (resume from suspend, driver reset, hostapd gave up);
- on single-channel radios, the WiFi connection moved to another channel;
- on single-channel radios, the WiFi connection has been gone for 20 s.
  The AP may be what stops NetworkManager reconnecting on another channel.

### Firewall backends record their own undo data

`net/firewall.py` chooses firewalld (if running), then iptables, then
nftables. Each backend's `apply` returns data its `revert` uses, so teardown
only removes what apsta added. For example, masquerading is removed only from
zones where apsta enabled it.

### No shell, no secrets in argv

`core/shell.run` takes argv lists only (no `shell=True` anywhere). Passwords
travel through files: hostapd.conf (0600), NetworkManager keyfiles in
`/run/NetworkManager/system-connections` (0600), and stdin
(`--password-stdin`), never command-line arguments that other users can read
in `ps`.

### The GUI is a CLI client

`apsta_gui/backend.py` runs `apsta status --json` unprivileged and
`pkexec /usr/bin/apsta <args>` for changes, never `pkexec sh -c`. The polkit
action `com.github.apsta.manage` (`auth_admin_keep`) names apsta in the
prompt. There is one implementation of every operation and one privilege
boundary.

## Extending

| Task                     | Where                                                                      |
| ------------------------ | -------------------------------------------------------------------------- |
| New hotspot method       | subclass `Strategy` in `net/strategies.py`, add to `STRATEGIES`             |
| New firewall             | class with `name/apply/revert` in `net/firewall.py`, register in `detect()` |
| New init system          | `ENABLE`/`DISABLE` in `cmd/service.py` + a file in `apsta_cli/data/`        |
| New USB chipset          | `USB_CHIPSET_DB` in `hw/usb.py`                                             |
| Card detected wrongly    | add its `iw phy` output to `tests/fixtures/iw/` with a test                 |
| New command              | parser in `cli.py`, handler in `cmd/`, logic in `services/`                 |

Shell completion is generated from the argparse tree, so new commands and
flags are completable without extra work.

## Testing

- `tests/unit/`: pure functions and modules with `FakeShell` (a scriptable
  stand-in for `core.shell.run`) and isolated paths (`tests/support.py`).
- `tests/integration/`: the real CLI, in-process, against a simulated
  network stack (`fakeworld.py`). That is fake `iw`, `ip`, `nmcli`, `hostapd`,
  `dnsmasq`, `iptables`, … executables that share state in a JSON file and a
  fake sysfs. These tests cover full start → clients → stop lifecycles,
  fallback with rollback, stale-state recovery, DFS refusal and more, with no
  root and no hardware.

Run `make test` or `make coverage`. CI enforces ≥ 90 % coverage.
