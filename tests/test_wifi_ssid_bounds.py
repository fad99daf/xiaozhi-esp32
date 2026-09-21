"""Regression checks for Wi-Fi SSID byte-boundary handling."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WIFI_BOARD_SOURCE = PROJECT_ROOT / "main/boards/common/wifi_board.cc"
WIFI_STATION_SOURCE = (
    PROJECT_ROOT / "components/78__esp-wifi-connect/wifi_station.cc"
)
TUYA_PROTOCOL_HEADER = PROJECT_ROOT / "main/protocols/tuya_protocol.h"
TUYA_PROTOCOL_SOURCE = PROJECT_ROOT / "main/protocols/tuya_protocol.cc"
APPLICATION_HEADER = PROJECT_ROOT / "main/application.h"
APPLICATION_SOURCE = PROJECT_ROOT / "main/application.cc"
AGENT_KIT_COMPONENT_CMAKE = PROJECT_ROOT / "components/esp-agentic-kit/CMakeLists.txt"


class TestWifiSsidBounds(unittest.TestCase):
    def test_agent_kit_component_links_device_reset_dependencies(self):
        """iot_client_reset needs the ATOP implementation in the ESP-IDF component."""
        source = AGENT_KIT_COMPONENT_CMAKE.read_text()
        self.assertIn("${AK_DIR}/modules/iot-client/src/iot_atop.c", source)

    def test_tuya_unbind_precedes_local_reprovisioning_wipe(self):
        """A bound device must receive cloud unbind confirmation before NVS is erased."""
        header = TUYA_PROTOCOL_HEADER.read_text()
        source = TUYA_PROTOCOL_SOURCE.read_text()
        self.assertIn("UnbindForWifiReprovisioning()", header)

        method_start = source.index("bool TuyaProtocol::UnbindForWifiReprovisioning()")
        method_end = source.index("bool TuyaProtocol::FetchToken()", method_start)
        method = source[method_start:method_end]
        self.assertIn("iot_client_reset(iot_client_, IOT_RESET_UNBIND_ONLY", method)
        self.assertIn("if (ret != OPRT_OK)", method)
        self.assertIn("iot_client_ = nullptr;", method)
        self.assertLess(
            method.index("if (ret != OPRT_OK)"),
            method.index("iot_client_ = nullptr;"),
        )

    def test_ble_reprovisioning_preserves_state_until_cloud_unbind_or_retry_limit(self):
        """A blocked cloud reset may fall back only after its persisted retry limit."""
        application_header = APPLICATION_HEADER.read_text()
        application_source = APPLICATION_SOURCE.read_text()
        board_source = WIFI_BOARD_SOURCE.read_text()
        self.assertIn("bool UnbindTuyaForWifiReprovisioning();", application_header)
        self.assertIn("UnbindForWifiReprovisioning()", application_source)

        function_start = board_source.index("void WifiBoard::EnterWifiConfigMode()")
        provisioning_start = board_source.index(
            "#if CONFIG_TUYA_BLE_PROVISIONING", function_start
        )
        provisioning_end = board_source.index("#endif", provisioning_start)
        provisioning = board_source[provisioning_start:provisioning_end]
        unbind = "app.UnbindTuyaForWifiReprovisioning()"
        wipe = 'Settings settings("tuya", true);'
        self.assertIn(unbind, provisioning)
        self.assertIn("SetBool(kTuyaReprovisionPendingKey, true)", provisioning)
        self.assertIn("SetInt(kTuyaReprovisionAttemptKey, attempt)", provisioning)
        self.assertLess(provisioning.index("SetBool(kTuyaReprovisionPendingKey, true)"), provisioning.index(unbind))
        self.assertIn("if (attempt >= kTuyaReprovisionMaxAttempts)", provisioning)
        self.assertIn("ForceLocalTuyaReprovisioning", provisioning)

    def test_password_protected_networks_allow_legacy_wpa_psk(self):
        """An unset ESP-IDF threshold silently defaults to WPA2 for 8+ byte passwords."""
        source = WIFI_STATION_SOURCE.read_text()
        self.assertIn(
            "ap_record.password.empty() ? WIFI_AUTH_OPEN : WIFI_AUTH_WPA_PSK",
            source,
        )

    def test_scan_miss_queues_saved_credentials_for_hidden_ap_fallback(self):
        """A hidden AP cannot be name-matched from scan results."""
        source = WIFI_STATION_SOURCE.read_text()
        self.assertIn(
            "No visible known AP found; trying saved credentials for hidden APs",
            source,
        )
        self.assertIn(".has_bssid = false", source)
        self.assertIn("if (remember_bssid_ && ap_record.has_bssid)", source)

    def test_ble_credentials_are_checked_before_persisting(self):
        """BLE credentials above the 32-byte Wi-Fi SSID limit must not reach NVS."""
        source = WIFI_BOARD_SOURCE.read_text()
        check = "ble_result.ssid.size() > 32"
        persist = "ssid_manager.AddSsid(ble_result.ssid, ble_result.password);"
        self.assertIn(check, source)
        self.assertLess(source.index(check), source.index(persist))

    def test_station_copy_preserves_a_full_32_byte_ssid_without_strcpy(self):
        """A valid 32-byte SSID has no room for a C-string terminator."""
        source = WIFI_STATION_SOURCE.read_text()
        self.assertNotIn("strcpy((char *)wifi_config.sta.ssid", source)
        self.assertIn(
            "memcpy(wifi_config.sta.ssid, ap_record.ssid.data(), ap_record.ssid.size());",
            source,
        )
        self.assertIn("ap_record.ssid.size() > sizeof(wifi_config.sta.ssid)", source)


if __name__ == "__main__":
    unittest.main()
