"""Exercise cloud pagination and URL resolution without hardware or MQTT."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CJSON = ROOT / "components/esp-agentic-kit/agentic-kit/third_party/cJSON"


class MusicCatalogHostTest(unittest.TestCase):
    def test_unbounded_correlation_scan_is_linear(self):
        source = (ROOT / "main/audio/music_catalog.cc").read_text()
        scan = source[source.index("if (url_response && count > 0)"):
                      source.index('if (cJSON_IsFalse(cJSON_GetObjectItem(data, "success"))')]
        self.assertIn("cJSON_ArrayForEach", scan)
        self.assertNotIn("cJSON_GetArrayItem", scan)

    def test_local_items_before_cloud_page_and_cancelled_responses(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            subprocess.run(["cc", "-std=c99", "-I", str(CJSON), "-c",
                            str(CJSON / "cJSON.c"), "-o", str(path / "cJSON.o")], check=True)
            subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-I", str(CJSON), "-I", str(ROOT / "main/audio"),
                            "-I", str(ROOT / "main/protocols"),
                            str(ROOT / "tests/music_catalog_test.cc"),
                            str(ROOT / "main/audio/music_catalog.cc"),
                            str(ROOT / "main/protocols/tuya_mqtt_skill.cc"),
                            str(path / "cJSON.o"), "-o", str(path / "test")], check=True)
            result = subprocess.run([str(path / "test")], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
