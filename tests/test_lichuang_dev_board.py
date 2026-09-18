"""Regression checks for Lichuang development-board defaults."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOARD_SOURCE = PROJECT_ROOT / "main/boards/lichuang-dev/lichuang_dev_board.cc"


class TestLichuangDevBoardDefaults(unittest.TestCase):
    def test_does_not_persist_a_temporary_quiet_environment_volume(self):
        """Board startup must retain the user's saved output-volume setting."""
        self.assertNotIn("SetOutputVolume(10)", BOARD_SOURCE.read_text())


if __name__ == "__main__":
    unittest.main()
