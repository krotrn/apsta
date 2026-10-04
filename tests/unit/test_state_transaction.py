import unittest
import unittest.mock as mock

from apsta_cli import state
from apsta_cli.core import paths
from apsta_cli.net.transaction import Transaction
from tests.support import isolate_paths


def sample(**kw):
    base = dict(
        method="hostapd",
        base_interface="wlo1",
        ap_interface="wlo1_ap",
        ssid="S",
        channel=6,
        band="bg",
        same_channel_required=True,
    )
    base.update(kw)
    return state.HotspotState(**base)


class StateTests(unittest.TestCase):
    def setUp(self):
        isolate_paths(self)

    def test_round_trip(self):
        st = sample(client_limits={"aa:bb:cc:dd:ee:ff": {"pref": 49152, "kbps": 100}})
        state.save(st)
        self.assertEqual(state.load(), st)
        self.assertEqual(oct(paths.STATE_PATH.stat().st_mode & 0o777), "0o644")
        state.clear()
        self.assertIsNone(state.load())

    def test_unknown_keys_ignored_and_corrupt_is_none(self):
        state.save(sample())
        paths.STATE_PATH.write_text(paths.STATE_PATH.read_text().replace('"method"', '"future_key": 1, "method"'))
        self.assertEqual(state.load().method, "hostapd")
        paths.STATE_PATH.write_text("{")
        self.assertIsNone(state.load())
        paths.STATE_PATH.write_text("{}")
        self.assertIsNone(state.load())

    def test_interface_exists(self):
        self.assertFalse(state.interface_exists("wlo1"))
        self.assertFalse(state.interface_exists(None))
        (paths.SYSFS_NET / "wlo1").mkdir()
        self.assertTrue(state.interface_exists("wlo1"))


class TransactionTests(unittest.TestCase):
    def test_rollback_in_reverse_on_error(self):
        order = []

        def failing_setup():
            with Transaction() as tx:
                tx.on_rollback("a", lambda: order.append("a"))
                tx.on_rollback("b", lambda: order.append("b"))
                raise RuntimeError("boom")

        self.assertRaises(RuntimeError, failing_setup)
        self.assertEqual(order, ["b", "a"])

    def test_commit_keeps_changes(self):
        undo = mock.Mock()
        with Transaction() as tx:
            tx.on_rollback("a", undo)
            tx.commit()
        undo.assert_not_called()

    def test_missing_commit_rolls_back(self):
        undo = mock.Mock()
        with Transaction() as tx:
            tx.on_rollback("a", undo)
        undo.assert_called_once()

    def test_failing_undo_does_not_stop_others(self):
        later = mock.Mock()
        with mock.patch("sys.stderr"):
            with Transaction() as tx:
                tx.on_rollback("first", later)
                tx.on_rollback("broken", mock.Mock(side_effect=OSError("x")))
        later.assert_called_once()


if __name__ == "__main__":
    unittest.main()
