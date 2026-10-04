# Security policy

apsta runs as root and changes firewall, routing and wireless settings, so we
take security reports seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private vulnerability
reporting (Security → Report a vulnerability) on
https://github.com/krotrn/apsta. We aim to acknowledge reports within a week.

## Supported versions

Only the latest release receives security fixes.

## Design notes

- Commands run as argv lists only; no shell interpolation of names, SSIDs or
  other external values.
- Hotspot passwords are stored in `/etc/apsta/secrets.json` (0600) and passed
  to hostapd/NetworkManager through 0600 files or stdin, never as command-line
  arguments. They are printed only to an interactive terminal (never to the
  journal), and logs never contain them.
- No default password: one is generated randomly on first start. The
  `changeme123` default of apsta ≤ 0.6 is treated as unset.
- The GUI escalates with `pkexec /usr/bin/apsta …` under the polkit action
  `com.github.apsta.manage`, never with a root shell.
- Runtime files live in `/run/apsta` (root-owned), not in world-writable `/tmp`.
- Changes apsta makes (firewall rules, `ip_forward`, interfaces) are recorded
  and reverted on stop.
