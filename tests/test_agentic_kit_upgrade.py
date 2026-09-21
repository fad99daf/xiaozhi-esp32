"""Regression checks for the agentic-kit Android BLE compatibility upgrade."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = PROJECT_ROOT / "components" / "esp-agentic-kit"


class TestAgenticKitUpgrade(unittest.TestCase):
    def test_sdk_logs_use_the_build_time_esp_idf_bridge(self):
        """New agentic-kit releases no longer expose the runtime log API."""
        config = (COMPONENT_ROOT / "kit_opts" / "agentic_kit_config.h").read_text()
        bridge = (COMPONENT_ROOT / "agentic_kit_log_bridge.c").read_text()
        cmake = (COMPONENT_ROOT / "CMakeLists.txt").read_text()

        self.assertIn("#define AGENTIC_KIT_LOG(", config)
        self.assertIn("agentic_kit_esp_log", config)
        self.assertIn("esp_log_writev", bridge)
        self.assertIn("contents redacted", bridge)
        self.assertIn('"${CMAKE_CURRENT_LIST_DIR}/kit_opts"', cmake)
        self.assertIn("agentic_kit_log_bridge.c", cmake)

    def test_app_does_not_call_removed_runtime_log_apis(self):
        """The app must remain buildable after switching to agentic-kit master."""
        sources = [
            PROJECT_ROOT / "main" / "protocols" / "tuya_protocol.cc",
            PROJECT_ROOT / "main" / "provisioning" / "tuya_ble_prov_wrapper.cc",
        ]
        removed_apis = ("log_set_handler(", "log_set_level(", "tai_set_log_level(")

        for source in sources:
            text = source.read_text()
            for api in removed_apis:
                self.assertNotIn(api, text, f"{source} still calls {api}")

    def test_component_builds_the_new_ota_verifier_source(self):
        """agentic-kit master moved OTA verification into its own source file."""
        cmake = (COMPONENT_ROOT / "CMakeLists.txt").read_text()
        self.assertIn("${AK_DIR}/modules/iot-client/src/iot_ota_verify.c", cmake)

    def test_ble_transport_accepts_android_protocol_version_two(self):
        """Android Tuya clients send v2 while the device advertises v4."""
        transport = (
            COMPONENT_ROOT / "agentic-kit" / "modules" / "tuya-ble" / "src"
            / "tuya_ble_trsmitr.c"
        ).read_text()

        self.assertIn("(ver_seq >> 4) < 2", transport)
        self.assertIn("minimum 2", transport)


if __name__ == "__main__":
    unittest.main()
