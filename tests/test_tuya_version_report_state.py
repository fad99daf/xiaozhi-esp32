"""Static regression checks for Tuya version-report startup policy."""

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main"


class TuyaVersionReportStateTest(unittest.TestCase):
    def test_state_is_versioned_and_fails_safe(self):
        source = (MAIN / "tuya_version_report_state.cc").read_text()
        self.assertIn('kReportedVersionKey[] = "version_report"', source)
        self.assertIn('kPendingVersionKey[] = "pending_report"', source)
        self.assertIn("reported_err != ESP_OK", source)
        self.assertIn("pending_err != ESP_OK", source)
        self.assertIn("std::strcmp(reported_version, current_version) != 0", source)
        self.assertIn("std::strcmp(pending_version, current_version) == 0", source)

    def test_long_lived_client_owns_report_consumption(self):
        source = (MAIN / "protocols/tuya_protocol.cc").read_text()
        self.assertIn("TuyaVersionReportState::ShouldReport(cfg.sw_ver)", source)
        self.assertIn("cfg.skip_version_report = !version_report_pending", source)
        self.assertIn("TuyaVersionReportState::MarkReported(cfg.sw_ver)", source)

    def test_first_binding_explicitly_reports(self):
        source = (MAIN / "protocols/tuya_protocol.cc").read_text()
        onboarding = source[source.index("bool TuyaProtocol::OnBoardWithToken"):]
        self.assertIn("cfg.skip_version_report = false", onboarding)
        self.assertIn("TuyaVersionReportState::MarkReported(cfg.sw_ver)", onboarding)

    def test_temporary_ota_client_skips_without_consuming(self):
        source = (MAIN / "ota.cc").read_text()
        check = source[source.index("bool Ota::CheckTuyaVersion"):]
        self.assertIn("cfg.skip_version_report = true", check)
        self.assertNotIn("MarkReported", check)

    def test_successful_firmware_install_marks_embedded_version_pending(self):
        source = (MAIN / "ota.cc").read_text()
        upgrade = source[source.index("bool Ota::Upgrade"):source.index("bool Ota::StartUpgrade")]
        set_boot = upgrade.index("esp_ota_set_boot_partition")
        mark_pending = upgrade.index("TuyaVersionReportState::MarkPending(target_version.c_str())")
        self.assertGreater(mark_pending, set_boot)

    def test_post_ota_report_precedes_next_upgrade_check(self):
        source = (MAIN / "application.cc").read_text()
        activation = source[source.index("void Application::ActivationTask"):source.index("void Application::CheckAssetsVersion")]
        self.assertLess(activation.index("TuyaVersionReportState::ShouldReport"),
                        activation.index("CheckNewVersion()"))
        self.assertLess(activation.index("InitializeProtocol()"),
                        activation.index("CheckNewVersion()"))

    def test_successful_report_clears_malformed_pending_state(self):
        source = (MAIN / "tuya_version_report_state.cc").read_text()
        mark_reported = source[source.index("bool MarkReported"):]
        self.assertIn("nvs_erase_key(handle, kPendingVersionKey)", mark_reported)

    def test_cloud_reset_erases_are_committed(self):
        source = (MAIN / "settings.cc").read_text()
        erase_all = source[source.index("void Settings::EraseAll"):]
        self.assertIn("dirty_ = true", erase_all)


if __name__ == "__main__":
    unittest.main()
