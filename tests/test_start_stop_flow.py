import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from apsta_cli.hardware import HardwareCapability, WifiInterface
from apsta_cli.net import start, stop


class StartStopFlowTests(unittest.TestCase):
    @patch("apsta_cli.net.start.save_config")
    @patch("apsta_cli.net.start._finalize_nmcli_start", return_value=True)
    @patch("apsta_cli.net.start._run_nmcli_hotspot")
    @patch("apsta_cli.net.start._create_virtual_ap_iface", return_value="wlan0_ap")
    @patch("apsta_cli.net.start._pick_least_congested_channel", return_value="6")
    @patch("apsta_cli.net.start._get_sta_channel_band", return_value=(None, None))
    @patch("apsta_cli.net.start.get_hardware_capability")
    @patch("apsta_cli.net.start.get_wifi_interfaces")
    @patch("apsta_cli.net.start.load_config")
    def test_start_prefers_nmcli_concurrent_path(
        self,
        mock_load_config,
        mock_get_ifaces,
        mock_get_cap,
        _mock_sta,
        _mock_pick,
        _mock_create,
        mock_nmcli,
        _mock_finalize,
        mock_save,
    ):
        cfg = {
            "ssid": "MyAP",
            "password": "secret123",
            "band": "bg",
            "channel": "11",
            "interface": "wlan0",
        }
        mock_load_config.return_value = cfg
        mock_get_ifaces.return_value = [WifiInterface(name="wlan0", mac="aa:bb", state="UP", connected_ssid="Home")]
        mock_get_cap.return_value = HardwareCapability(
            interface="wlan0",
            supports_ap=True,
            supports_sta=True,
            supports_ap_sta_concurrent=True,
            supports_ap_sta_split=True,
            max_interfaces=2,
            supported_modes=["managed", "AP"],
            combinations=["combo"],
            driver="iwlwifi",
            chipset="Intel",
        )
        mock_nmcli.return_value = subprocess.CompletedProcess(args=["nmcli"], returncode=0, stdout="", stderr="")

        start._cmd_start_impl(SimpleNamespace(force=False))

        self.assertGreaterEqual(mock_save.call_count, 1)
        final_cfg = mock_save.call_args_list[-1][0][0]
        self.assertEqual(final_cfg.get("start_method"), "nmcli")

    @patch("apsta_cli.net.start._pick_least_congested_channel", return_value=None)
    @patch("apsta_cli.net.start._get_sta_channel_band", return_value=(None, None))
    @patch("apsta_cli.net.start._get_connected_ssid", return_value="Home")
    @patch("apsta_cli.net.start.get_hardware_capability")
    @patch("apsta_cli.net.start.get_wifi_interfaces")
    @patch("apsta_cli.net.start.load_config")
    def test_start_requires_force_when_no_concurrent_support(
        self,
        mock_load_config,
        mock_get_ifaces,
        mock_get_cap,
        _mock_connected,
        _mock_sta,
        _mock_pick,
    ):
        mock_load_config.return_value = {
            "ssid": "MyAP",
            "password": "secret123",
            "band": "bg",
            "channel": "11",
            "interface": "wlan0",
        }
        mock_get_ifaces.return_value = [WifiInterface(name="wlan0", mac="aa:bb", state="UP", connected_ssid="Home")]
        mock_get_cap.return_value = HardwareCapability(
            interface="wlan0",
            supports_ap=True,
            supports_sta=True,
            supports_ap_sta_concurrent=False,
            supports_ap_sta_split=False,
            max_interfaces=1,
            supported_modes=["managed", "AP"],
            combinations=["combo"],
            driver="iwlwifi",
            chipset="Intel",
        )

        with self.assertRaises(SystemExit) as ctx:
            start._cmd_start_impl(SimpleNamespace(force=False))
        self.assertEqual(ctx.exception.code, 1)

    @patch("apsta_cli.net.stop.save_config")
    @patch("apsta_cli.net.stop._stop_hostapd_ap_sta")
    @patch("apsta_cli.net.stop.load_config")
    @patch("apsta_cli.net.stop.require_root")
    def test_stop_hostapd_flow_clears_runtime_state(self, _mock_root, mock_load, mock_stop_hostapd, mock_save):
        cfg = {
            "start_method": "hostapd",
            "ap_interface": "wlan0_ap",
            "base_interface": "wlan0",
            "active_con_name": None,
        }
        mock_load.return_value = cfg

        stop._cmd_stop_impl(SimpleNamespace())

        mock_stop_hostapd.assert_called_once_with("wlan0_ap", "wlan0")
        self.assertEqual(cfg["ap_interface"], None)
        self.assertEqual(cfg["base_interface"], None)
        self.assertEqual(cfg["active_con_name"], None)
        self.assertEqual(cfg["start_method"], None)
        mock_save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
