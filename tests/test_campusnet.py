import sys
import unittest
from http.client import RemoteDisconnected
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import campusnet


class SRunEncodingTests(unittest.TestCase):
    def test_custom_base64_matches_bit_portal_alphabet(self) -> None:
        self.assertEqual(
            campusnet.srun_base64_encode(b"CampusNet SRun info"),
            "+IifMNuzFUut2i0mnaDSOaeUWv==",
        )

    def test_xencode_and_custom_base64_match_browser_reference(self) -> None:
        payload = '{"username":"test","password":"pass","ip":"10.1.2.3","acid":"8","enc_ver":"srun_bx1"}'
        token = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        encoded = campusnet.srun_base64_encode(campusnet.srun_xencode(payload, token))
        self.assertEqual(
            encoded,
            "bBkzWXCkz1O2rY3owbSjTm17PM20o0rVW89iXDg6ZX32BwIwIE5hF7oOkB9mjATYVbCOIx7LDq+5mfdflJa6VgDttR+g3/GYErDMb/bvs1KlGlb1kSuWByxL0s4=",
        )

    def test_invalid_alphabet_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            campusnet.srun_base64_encode(b"data", "A" * 64)

    def test_remote_disconnect_is_classified_as_transport_failure(self) -> None:
        with patch("campusnet.urlopen", side_effect=RemoteDisconnected("closed")):
            with self.assertRaises(campusnet.PortalTransportError):
                campusnet.jsonp("http://10.0.0.55/cgi-bin/get_challenge", {})

    def test_forced_reconnect_resets_wifi_before_checking_network(self) -> None:
        config = {"wifi": {"ssid": "BIT-Web", "connect_wait_seconds": 1}}
        connected = campusnet.WifiStatus("WLAN 2", "BIT-Web", True)
        with (
            patch("campusnet.wifi_status", return_value=connected),
            patch("campusnet.disconnect_wifi", return_value=True) as disconnect,
            patch("campusnet.connect_wifi", return_value=True) as connect,
            patch("campusnet.wait_for_wifi", return_value=connected),
            patch("campusnet.internet_available", return_value=True),
            patch("campusnet.time.sleep"),
            patch.object(campusnet.LOG, "warning"),
            patch.object(campusnet.LOG, "info"),
        ):
            result = campusnet.ensure_connected(config, force_wifi_reconnect=True)

        self.assertTrue(result.healthy)
        disconnect.assert_called_once_with()
        connect.assert_called_once_with("BIT-Web")

    def test_wait_for_wifi_requires_connected_state_and_matching_ssid(self) -> None:
        statuses = [
            campusnet.WifiStatus("WLAN 2", "BIT-Web", False),
            campusnet.WifiStatus("WLAN 2", "other", True),
            campusnet.WifiStatus("WLAN 2", "BIT-Web", True),
        ]
        with (
            patch("campusnet.wifi_status", side_effect=statuses),
            patch("campusnet.time.monotonic", side_effect=[0, 0, 1, 1, 2]),
            patch("campusnet.time.sleep"),
            patch.object(campusnet.LOG, "info"),
        ):
            status = campusnet.wait_for_wifi("BIT-Web", 10)
        self.assertEqual(status, statuses[-1])

    def test_recovery_cooldown_uses_bounded_exponential_backoff(self) -> None:
        config = {
            "wifi": {
                "wifi_reconnect_cooldown_seconds": 30,
                "max_wifi_reconnect_cooldown_seconds": 300,
            }
        }
        self.assertEqual(campusnet.wifi_recovery_cooldown(config, 1), 30)
        self.assertEqual(campusnet.wifi_recovery_cooldown(config, 2), 60)
        self.assertEqual(campusnet.wifi_recovery_cooldown(config, 5), 300)

    def test_forced_reconnect_can_renew_dhcp_after_association(self) -> None:
        config = {"wifi": {"ssid": "BIT-Web", "connect_wait_seconds": 1}}
        connected = campusnet.WifiStatus("WLAN 2", "BIT-Web", True)
        with (
            patch("campusnet.wifi_status", return_value=connected),
            patch("campusnet.disconnect_wifi", return_value=True),
            patch("campusnet.connect_wifi", return_value=True),
            patch("campusnet.wait_for_wifi", return_value=connected),
            patch("campusnet.renew_dhcp_lease", return_value=True) as renew,
            patch("campusnet.internet_available", return_value=True),
            patch("campusnet.time.sleep"),
            patch.object(campusnet.LOG, "warning"),
            patch.object(campusnet.LOG, "info"),
        ):
            result = campusnet.ensure_connected(config, force_wifi_reconnect=True, renew_dhcp=True)
        self.assertTrue(result.healthy)
        renew.assert_called_once_with("WLAN 2")

    def test_disconnected_wifi_checks_software_radio_before_connecting(self) -> None:
        config = {"wifi": {"ssid": "BIT-Web", "connect_wait_seconds": 1}}
        disconnected = campusnet.WifiStatus("WLAN 2", None, False)
        connected = campusnet.WifiStatus("WLAN 2", "BIT-Web", True)
        with (
            patch("campusnet.wifi_status", return_value=disconnected),
            patch("campusnet.enable_powered_down_wifi_radios", return_value=True) as enable_radio,
            patch("campusnet.connect_wifi", return_value=True) as connect,
            patch("campusnet.wait_for_wifi", return_value=connected),
            patch("campusnet.internet_available", return_value=True),
            patch("campusnet.time.sleep") as sleep,
            patch.object(campusnet.LOG, "warning"),
            patch.object(campusnet.LOG, "info"),
        ):
            result = campusnet.ensure_connected(config)
        self.assertTrue(result.healthy)
        enable_radio.assert_called_once_with()
        connect.assert_called_once_with("BIT-Web")
        sleep.assert_called_once_with(2)


if __name__ == "__main__":
    unittest.main()
