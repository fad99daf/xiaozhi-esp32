"""Regression checks for Lichuang development-board defaults."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOARD_SOURCE = PROJECT_ROOT / "main/boards/lichuang-dev/lichuang_dev_board.cc"


class TestLichuangDevBoardDefaults(unittest.TestCase):
    def test_long_press_schedules_wifi_reprovisioning_on_main_task(self):
        """Long-press callbacks run in esp_timer and must not do network teardown."""
        source = BOARD_SOURCE.read_text()
        self.assertIn("boot_button_.OnLongPress([this]()", source)
        long_press_start = source.index("boot_button_.OnLongPress([this]()")
        long_press_end = source.index("        });\n\n#if CONFIG_USE_DEVICE_AEC", long_press_start)
        long_press = source[long_press_start:long_press_end]
        schedule = "Application::GetInstance().Schedule([this]()"
        self.assertIn(schedule, long_press)
        self.assertIn("EnterWifiConfigMode();", long_press)
        self.assertLess(long_press.index(schedule), long_press.index("EnterWifiConfigMode();"))

    def test_does_not_persist_a_temporary_quiet_environment_volume(self):
        """Board startup must retain the user's saved output-volume setting."""
        self.assertNotIn("SetOutputVolume(10)", BOARD_SOURCE.read_text())


if __name__ == "__main__":
    unittest.main()
