import sys
import subprocess
import unittest
from http.client import RemoteDisconnected
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError


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

    def test_fast_network_check_uses_only_one_probe_and_short_timeout(self) -> None:
        config = {
            "network_check": {
                "probes": [{"url": "http://first"}, {"url": "http://second"}],
                "timeout_seconds": 8,
                "retry_timeout_seconds": 2,
            }
        }
        with patch("campusnet.urlopen", side_effect=URLError("offline")) as urlopen:
            self.assertFalse(campusnet.internet_available(config, fast=True))
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 2)

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

    def test_winrt_radio_switch_reports_when_it_turns_wifi_on(self) -> None:
        completed = subprocess.CompletedProcess(["powershell"], 0, stdout="ENABLED\n", stderr="")
        with (
            patch("campusnet.subprocess.run", return_value=completed) as run,
            patch.object(campusnet.LOG, "warning") as warning,
        ):
            self.assertTrue(campusnet.enable_powered_down_wifi_radios())
        self.assertEqual(run.call_args.args[0][0], "powershell")
        warning.assert_called_once()

    def test_winrt_radio_switch_noops_when_wifi_is_already_on(self) -> None:
        completed = subprocess.CompletedProcess(["powershell"], 0, stdout="ALREADY_ON\n", stderr="")
        with patch("campusnet.subprocess.run", return_value=completed):
            self.assertFalse(campusnet.enable_powered_down_wifi_radios())

    def test_successful_wifi_recovery_clears_state_without_scheduling_another_one(self) -> None:
        config = {
            "wifi": {
                "ssid": "BIT-Web",
                "reconnect_after_portal_failures": 3,
                "dhcp_renew_after_wifi_recoveries": 2,
                "adapter_reset_after_wifi_recoveries": 5,
            },
            "check_interval_seconds": 60,
            "retry_interval_seconds": 10,
        }
        attempts = [
            campusnet.ConnectionAttempt(False, portal_transport_failure=True),
            campusnet.ConnectionAttempt(False, portal_transport_failure=True),
            campusnet.ConnectionAttempt(False, portal_transport_failure=True),
            campusnet.ConnectionAttempt(True),
        ]
        with (
            patch("campusnet.acquire_single_instance", return_value=True),
            patch("campusnet.load_config", return_value=config),
            patch("campusnet.ensure_connected", side_effect=attempts) as ensure,
            patch("campusnet.wifi_recovery_cooldown") as cooldown,
            patch("campusnet.time.sleep", side_effect=[None, None, None, KeyboardInterrupt]),
            patch.object(campusnet.LOG, "info") as info,
            patch.object(sys, "argv", ["campusnet.py"]),
        ):
            with self.assertRaises(KeyboardInterrupt):
                campusnet.main()

        self.assertEqual(
            [call.kwargs["force_wifi_reconnect"] for call in ensure.call_args_list],
            [False, False, False, True],
        )
        self.assertEqual(
            [call.kwargs["fast_network_check"] for call in ensure.call_args_list],
            [False, True, True, True],
        )
        cooldown.assert_not_called()
        self.assertIn(
            "网络已恢复正常，已清除失败计数；不会再执行计划中的 Wi-Fi 物理恢复。",
            [call.args[0] for call in info.call_args_list],
        )


if __name__ == "__main__":
    unittest.main()
