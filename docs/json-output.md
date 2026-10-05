# Machine-readable output

Commands that support `--json` print a single JSON document on stdout. The GUI
is built on this output, and scripts can use it too.

**Stability:** keys may be added in any release. Renaming or removing a key is
a breaking change and is listed in the [changelog](https://github.com/krotrn/apsta/blob/main/CHANGELOG.md). Ignore
keys you don't know. Errors go to stderr with a non-zero exit code (see
[Exit codes](#exit-codes)).

Values marked *root* are only present, or only accurate, when run with root
privileges.

## `apsta status --json`

Current hotspot, connected devices and configuration. Works without root.

```json
{
  "active": true,
  "stale": false,
  "hotspot": {
    "method": "hostapd",
    "base_interface": "wlo1",
    "ap_interface": "wlo1_ap",
    "ssid": "MyHotspot",
    "channel": 6,
    "band": "bg",
    "same_channel_required": true,
    "sta_ssid_at_start": "Home",
    "started_at": "2026-10-05T09:12:44.120931+00:00",
    "subnet": "192.168.42.0/24",
    "gateway": "192.168.42.1",
    "supervisor": "systemd",
    "firewall": {"backend": "iptables", "ip_forward_prev": "0", "data": {}},
    "connection_id": null,
    "client_limits": {"aa:bb:cc:dd:ee:ff": {"pref": 49152, "kbps": 8000}},
    "blocked": [],
    "notes": [
      "Method hostapd: the best one for this card and setup.",
      "Channel 6 (2.4 GHz): the same as your WiFi's (this card uses one channel for both)."
    ]
  },
  "clients": [
    {"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.42.17", "hostname": "pixel-8", "limit_kbps": 8000, "blocked": false}
  ],
  "interfaces": [
    {"name": "wlo1", "mac": "e4:…", "phy": "phy0", "type": "managed", "state": "UP", "connected_ssid": "Home"}
  ],
  "autostart": {"init": "systemd", "enabled": true, "running": true},
  "config": {
    "active_profile": "default",
    "profiles": ["default", "travel"],
    "ssid": "MyHotspot",
    "band": "bg",
    "channel": "auto",
    "method": "auto",
    "interface": null
  }
}
```

| Key           | Meaning |
| ------------- | ------- |
| `active`      | A hotspot is running and healthy. |
| `stale`       | A hotspot was recorded but is gone (crash, driver reset). The next `start` cleans it up. |
| `hotspot`     | The running hotspot, or `null`. `method` is `hostapd`, `nmcli`, `p2p` or `nmcli-single`. `band` is `bg` (2.4 GHz) or `a` (5 GHz). `subnet`/`gateway`/`firewall`/`supervisor` are set in hostapd and p2p mode (`p2p_backend` and `p2p_network` too in p2p mode), `connection_id` in nmcli modes. `firewall` and `client_limits` are internal bookkeeping for `stop`; don't rely on their contents. `notes` explains the choices made at start (method, channel, settings that couldn't be followed), one sentence each, for display. |
| `clients`     | Currently associated devices (not stale DHCP leases). `ip` and `hostname` may be empty. |
| `interfaces`  | WiFi interfaces. `type` is the nl80211 interface type (`managed`, `AP`, …). |
| `autostart`   | `init` is `systemd`, `openrc`, `runit` or `unknown`. `enabled`/`running` are `null` when unknown. |
| `config`      | The active profile's settings (no password). `interface: null` means automatic; `channel` and `method` may be `auto`. |

`apsta status --check` prints nothing and exits `0` if the hotspot is running,
`3` if not.

## `apsta detect --json`

What the WiFi hardware can do and which method apsta would use.

```json
{
  "interfaces": [{"name": "wlo1", "mac": "e4:…", "phy": "phy0", "type": "managed", "state": "UP", "connected_ssid": "Home"}],
  "target_interface": "wlo1",
  "capability": {
    "interface": "wlo1",
    "phy": "phy0",
    "supports_ap": true,
    "supports_sta": true,
    "ap_sta": true,
    "same_channel_required": true,
    "max_channels": 1,
    "ap_frequencies": [2412, 2437, 2462, 5745, 5765],
    "supported_modes": ["managed", "AP", "monitor"],
    "combinations": ["#{ managed } <= 1, #{ AP, P2P-client, P2P-GO } <= 1, … total <= 3, #channels <= 1"],
    "driver": "iwlwifi",
    "chipset": "Intel Corporation Wi-Fi 6 AX201",
    "p2p_go_own_channel": true
  },
  "methods": {"hostapd": "ready", "nmcli": "ready", "p2p": "ready"},
  "verdict": {
    "level": "ok",
    "mode": "ap+sta",
    "messages": ["Your card can run a hotspot while staying connected to WiFi."],
    "next": "sudo apsta start"
  }
}
```

| Key            | Meaning |
| -------------- | ------- |
| `capability.ap_sta` | The radio can run an access point and a WiFi connection at the same time. |
| `capability.same_channel_required` | If so, the hotspot must use the WiFi connection's channel. |
| `capability.p2p_go_own_channel` | A Wi-Fi Direct group may use a different channel than the WiFi connection (the `p2p` method's fallback). |
| `methods`      | `"ready"`, or what's missing (e.g. `"needs hostapd, dnsmasq"`). `p2p` is listed only on cards that support it. |
| `verdict.mode` | `ap+sta` (keeps WiFi), `single` (hotspot drops WiFi) or `unsupported`. `level` is `ok`, `warn` or `error`. `warnings` (optional) explains a `warn`, e.g. the WiFi is on a channel this card can't host on. |

## `apsta config --json`

The active profile's settings.

```json
{
  "active_profile": "default",
  "profiles": ["default", "travel"],
  "settings": {"ssid": "MyHotspot", "band": "bg", "channel": "auto", "method": "auto", "interface": null},
  "password": "kd7Ws3qPzT9mXbR2"
}
```

`password` is included only with `--show-password`, which requires root.

## `apsta clients --json`

The same list as `clients` in `status --json`.

## `apsta start --json`

```json
{"hotspot": { "...": "same object as status.hotspot" }, "method": "hostapd"}
```

## Exit codes

| Code | Meaning |
| ---- | ------- |
| 0    | Success |
| 1    | Failed (hardware, setup or other error; message on stderr) |
| 2    | Invalid arguments or configuration value |
| 3    | Hotspot already running (`start`), or not running (`status --check`) |
| 4    | Needs root |
| 130  | Interrupted |
