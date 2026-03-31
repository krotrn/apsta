import unittest
from unittest.mock import patch

from apsta_cli import hardware


class HardwareCapabilityTests(unittest.TestCase):
    def _run_out_side_effect(self, iw_text, lspci_text="", lsusb_text=""):
        def _inner(cmd):
            if cmd == "iw list":
                return iw_text
            if cmd == "lspci | grep -i wireless":
                return lspci_text
            if cmd == "lsusb | grep -i wireless":
                return lsusb_text
            return ""
        return _inner

    @patch("apsta_cli.hardware.os.readlink", return_value="/drivers/iwlwifi")
    @patch("apsta_cli.hardware.os.path.islink", return_value=True)
    @patch("apsta_cli.hardware.run_out")
    def test_capability_detects_ap_sta_same_group(self, mock_run_out, _islink, _readlink):
        iw_text = """
Wiphy phy0
Supported interface modes:
\t * managed
\t * AP
valid interface combinations:
\t * #{ managed, AP } <= 2, total <= 2, #channels <= 1
"""
        mock_run_out.side_effect = self._run_out_side_effect(iw_text, lspci_text="Network controller: Intel AX200")

        cap = hardware.get_hardware_capability("wlan0")
        self.assertTrue(cap.supports_ap)
        self.assertTrue(cap.supports_sta)
        self.assertTrue(cap.supports_ap_sta_concurrent)
        self.assertFalse(cap.supports_ap_sta_split)
        self.assertEqual(cap.max_interfaces, 2)
        self.assertEqual(cap.driver, "iwlwifi")

    @patch("apsta_cli.hardware.os.path.islink", return_value=False)
    @patch("apsta_cli.hardware.run_out")
    def test_capability_detects_ap_sta_split_group(self, mock_run_out, _islink):
        iw_text = """
Wiphy phy0
Supported interface modes:
\t * managed
\t * AP
valid interface combinations:
\t * #{ managed } <= 1, #{ AP } <= 1, total <= 2, #channels <= 1
"""
        mock_run_out.side_effect = self._run_out_side_effect(iw_text)

        cap = hardware.get_hardware_capability("wlan0")
        self.assertTrue(cap.supports_ap)
        self.assertTrue(cap.supports_sta)
        self.assertFalse(cap.supports_ap_sta_concurrent)
        self.assertTrue(cap.supports_ap_sta_split)
        self.assertEqual(cap.max_interfaces, 2)


if __name__ == "__main__":
    unittest.main()
