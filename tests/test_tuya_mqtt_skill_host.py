"""Exercise the MQTT skill-card envelope parser with the ESP-IDF cJSON source."""

from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CJSON = ROOT / "components/esp-agentic-kit/agentic-kit/third_party/cJSON"


class TuyaMqttSkillHostTest(unittest.TestCase):
    def test_real_iot_and_legacy_envelopes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cjson_object = Path(temp_dir) / "cJSON.o"
            binary = Path(temp_dir) / "tuya_mqtt_skill_test"
            subprocess.run([
                "cc", "-std=c99", "-I", str(CJSON), "-c",
                str(CJSON / "cJSON.c"), "-o", str(cjson_object),
            ], check=True, capture_output=True, text=True)
            subprocess.run([
                "c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                "-I", str(CJSON), "-I", str(ROOT / "main/protocols"),
                str(ROOT / "tests/tuya_mqtt_skill_test.cc"),
                str(ROOT / "main/protocols/tuya_mqtt_skill.cc"),
                str(cjson_object), "-lm", "-o", str(binary),
            ], check=True, capture_output=True, text=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
