"""All-or-nothing setup: each completed step registers how to undo itself.

    with Transaction() as tx:
        create_iface(); tx.on_rollback("delete iface", lambda: delete_iface())
        start_daemon(); tx.on_rollback("stop daemon", stop_daemon)
        tx.commit()

If anything raises before ``commit()``, the registered undo steps run in
reverse order. A failing undo step is reported and doesn't stop the others.
"""

from __future__ import annotations

from typing import Callable, List, Tuple

from ..core import output


class Transaction:
    def __init__(self) -> None:
        self._undo: List[Tuple[str, Callable[[], object]]] = []
        self._committed = False

    def on_rollback(self, description: str, action: Callable[[], object]) -> None:
        self._undo.append((description, action))

    def commit(self) -> None:
        self._committed = True
        self._undo.clear()

    def rollback(self) -> None:
        while self._undo:
            description, action = self._undo.pop()
            output.dbg("Rolling back", step=description)
            try:
                action()
            except Exception as exc:  # noqa: BLE001 - keep undoing the rest
                output.warn(f"Cleanup step '{description}' failed: {exc}")

    def __enter__(self) -> Transaction:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None or not self._committed:
            self.rollback()
        return False
