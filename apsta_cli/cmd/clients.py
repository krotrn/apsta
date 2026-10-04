"""clients: list, disconnect/block, unblock, rate-limit."""

from __future__ import annotations

import json
from typing import List

from .. import state as state_store
from ..core import lock, output
from ..core.errors import UsageError
from ..net import clients
from ..services import hotspot


def print_clients(items: List[dict], active: bool) -> None:
    output.head("apsta — Connected clients")
    output.blank()
    if not active:
        output.info("The hotspot is not running.")
    elif not items:
        output.info("No clients connected.")
    else:
        output.detail(f"{'HOSTNAME':<20} {'MAC':<18} {'IP':<16} LIMIT")
        for c in items:
            limit = f"{c['limit_kbps']} Kbps" if c.get("limit_kbps") else "-"
            output.detail(f"{(c['hostname'] or '-')[:20]:<20} {c['mac']:<18} {c['ip'] or '-':<16} {limit}")
    output.blank()


def cmd_clients(args) -> int:
    action = getattr(args, "action", None) or "list"
    if action == "list":
        data = hotspot.status()
        if args.json:
            print(json.dumps(data["clients"], indent=2))
        else:
            print_clients(data["clients"], data["active"])
        return 0

    with lock.command_lock("clients"):
        st = hotspot.with_running_state("Managing clients")
        target = clients.resolve(clients.list_clients(st), args.client)

        if action == "disconnect":
            clients.disconnect(st, target.mac, block=args.block)
            output.ok(f"{'Blocked' if args.block else 'Disconnected'} {target.mac}")
        elif action == "unblock":
            clients.unblock(st, target.mac)
            output.ok(f"Unblocked {target.mac}")
        elif action == "limit":
            if args.kbps is None:
                raise UsageError("A limit in Kbps is required.")
            clients.set_limit(st, target.mac, int(args.kbps))
            output.ok(f"Limited {target.mac} to {args.kbps} Kbps")
        elif action == "unlimit":
            if clients.clear_limit(st, target.mac):
                output.ok(f"Removed the limit for {target.mac}")
            else:
                output.info(f"{target.mac} had no limit.")
        else:
            raise UsageError(f"Unknown clients action: {action}")
        state_store.save(st)
    return 0
