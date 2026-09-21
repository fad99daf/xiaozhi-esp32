"""Regression checks for recoverable Tuya Wi-Fi reprovisioning."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WIFI_BOARD_HEADER = PROJECT_ROOT / "main/boards/common/wifi_board.h"
WIFI_BOARD_SOURCE = PROJECT_ROOT / "main/boards/common/wifi_board.cc"
APPLICATION_SOURCE = PROJECT_ROOT / "main/application.cc"
SETTINGS_SOURCE = PROJECT_ROOT / "main/settings.cc"


class TestWifiReprovisionRecovery(unittest.TestCase):
    def test_unbind_attempt_is_persisted_before_calling_the_cloud(self):
        """A reboot during a blocked unbind call must resume from NVS."""
        source = WIFI_BOARD_SOURCE.read_text()
        start = source.index("void WifiBoard::EnterWifiConfigMode()")
        end = source.index("bool WifiBoard::IsInWifiConfigMode()", start)
        flow = source[start:end]

        pending = 'SetBool(kTuyaReprovisionPendingKey, true)'
        attempt = 'SetInt(kTuyaReprovisionAttemptKey, attempt)'
        unbind = 'app.UnbindTuyaForWifiReprovisioning()'
        self.assertIn(pending, flow)
        self.assertIn(attempt, flow)
        self.assertIn(unbind, flow)
        self.assertLess(flow.index(pending), flow.index(unbind))
        self.assertLess(flow.index(attempt), flow.index(unbind))

    def test_unbind_has_a_timeout_and_forces_local_cleanup_after_three_attempts(self):
        """A cloud-side success that never returns locally cannot brick reprovisioning."""
        header = WIFI_BOARD_HEADER.read_text()
        source = WIFI_BOARD_SOURCE.read_text()
        self.assertIn("reprovision_timeout_timer_", header)
        self.assertIn("OnTuyaReprovisionTimeout", header)
        self.assertIn("kTuyaReprovisionMaxAttempts = 3", source)
        self.assertIn("esp_timer_start_once(reprovision_timeout_timer_", source)
        self.assertIn("void WifiBoard::OnTuyaReprovisionTimeout", source)
        self.assertIn("if (attempt >= kTuyaReprovisionMaxAttempts)", source)
        self.assertIn("ForceLocalTuyaReprovisioning", source)

    def test_activation_resumes_a_pending_reprovisioning_request(self):
        """Retries after a reboot wait for the protocol to finish activating."""
        header = WIFI_BOARD_HEADER.read_text()
        source = WIFI_BOARD_SOURCE.read_text()
        application = APPLICATION_SOURCE.read_text()
        self.assertIn("ResumePendingTuyaReprovisioning", header)
        self.assertIn("bool WifiBoard::ResumePendingTuyaReprovisioning()", source)
        self.assertIn("ResumePendingTuyaReprovisioning()", application)

    def test_erasing_tuya_state_marks_the_nvs_handle_for_commit(self):
        """Forced recovery must survive the reboot that follows it."""
        source = SETTINGS_SOURCE.read_text()
        start = source.index("void Settings::EraseAll()")
        method = source[start:]
        self.assertIn("nvs_erase_all", method)
        self.assertIn("dirty_ = true;", method)


if __name__ == "__main__":
    unittest.main()
