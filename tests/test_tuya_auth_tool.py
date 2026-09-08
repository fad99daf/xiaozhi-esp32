"""Host tests for scripts/tuya_auth_tool.py.

Covers: authkey file parsing, validation, CSV generation, and a full
generate->parse round trip using the real IDF nvs tools (no device needed).
The flash/read device paths are exercised through mocked subprocess/parttool.
"""

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "scripts"))

import tuya_auth_tool  # noqa: E402

IDF_PATH = os.environ.get("IDF_PATH", os.path.expanduser("~/esp/esp-idf"))


def idf_env_available():
    gen = os.path.join(IDF_PATH, "components", "partition_table", "parttool.py")
    parser = os.path.join(IDF_PATH, "components", "nvs_flash",
                          "nvs_partition_tool", "nvs_parser.py")
    return os.path.isfile(gen) and os.path.isfile(parser)


@unittest.skipUnless(idf_env_available(), "ESP-IDF not found")
class TestNvsRoundTrip(unittest.TestCase):
    """Real generate -> parse round trip (16 KB image, like the default nvs)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tuya_auth_rt_")
        self.addCleanup(self.tmp.cleanup)
        os.environ["IDF_PATH"] = IDF_PATH

    def generate_and_parse(self, values):
        csv_path = os.path.join(self.tmp.name, "t.csv")
        bin_path = os.path.join(self.tmp.name, "t.bin")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write(tuya_auth_tool.values_to_nvs_csv(values))
        import subprocess as sp
        res = sp.run(
            [sys.executable, "-m", "esp_idf_nvs_partition_gen",
             "generate", csv_path, bin_path, "0x4000"],
            capture_output=True, text=True,
            env={**os.environ, "IDF_PATH": IDF_PATH})
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        return tuya_auth_tool.parse_nvs_dump(bin_path)

    def test_round_trip_preserves_all_fields(self):
        values = {
            "uuid": "uuid-roundtrip-123",
            "auth_key": "x" * 32,
            "product_key": "pkroundtrip12345",
        }
        stored = self.generate_and_parse(values)[tuya_auth_tool.NAMESPACE]
        for key, expected in values.items():
            self.assertEqual(stored[key], expected)

    def test_round_trip_special_chars(self):
        values = {
            "uuid": "uuid-with.dots-and_underscore",
            "auth_key": "B64chars+/=aes" + "0" * 18,
            "product_key": "pk_1234567890abc",
        }
        stored = self.generate_and_parse(values)[tuya_auth_tool.NAMESPACE]
        for key, expected in values.items():
            self.assertEqual(stored[key], expected)


class TestParsingAndValidation(unittest.TestCase):
    def test_parse_authkey_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("TUYA_UUID=u1234567890123456\n"
                    "\n"
                    "# comment line\n"
                    "TUYA_AUTH_KEY=k" + "x" * 31 + "\n"
                    "TUYA_PRODUCT_KEY=pk12345678901234\n")
            path = f.name
        self.addCleanup(os.unlink, path)
        values = tuya_auth_tool.parse_authkey_file(path)
        self.assertEqual(values["uuid"], "u1234567890123456")
        self.assertEqual(values["auth_key"], "k" + "x" * 31)
        self.assertEqual(values["product_key"], "pk12345678901234")

    def test_parse_rejects_garbage_line(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("NOT_A_KEY_VALUE\n")
            path = f.name
        self.addCleanup(os.unlink, path)
        with self.assertRaises(SystemExit):
            tuya_auth_tool.parse_authkey_file(path)

    def _args(self, **kwargs):
        namespace = {"uuid": None, "auth_key": None, "pid": None}
        namespace.update(kwargs)
        return types.SimpleNamespace(**namespace)

    def test_apply_overrides_and_validate_ok(self):
        args = self._args(uuid="u-override-123456", auth_key="k" + "x" * 31)
        values = {"uuid": "u1234567890123456",
                  "auth_key": "k" + "x" * 31,
                  "product_key": "pk12345678901234"}
        tuya_auth_tool.apply_overrides(values, args)
        self.assertEqual(values["uuid"], "u-override-123456")

    def test_apply_overrides_missing_field_exits(self):
        args = self._args()
        values = {"uuid": "u1234567890123456"}  # auth_key/product_key missing
        with self.assertRaises(SystemExit):
            tuya_auth_tool.apply_overrides(values, args)

    def test_apply_overrides_too_long_exits(self):
        args = self._args(uuid="u" * 32)  # max is 31
        values = {"uuid": "u" * 32, "auth_key": "k" * 32, "product_key": "p" * 16}
        with self.assertRaises(SystemExit):
            tuya_auth_tool.apply_overrides(values, args)

    def test_csv_structure(self):
        csv_text = tuya_auth_tool.values_to_nvs_csv(
            {"uuid": "u1234567890123456", "auth_key": "k" * 32,
             "product_key": "p" * 16})
        lines = csv_text.strip().split("\n")
        self.assertEqual(lines[0], "key,type,encoding,value")
        self.assertEqual(lines[1], "tuya_auth,namespace,,")
        self.assertEqual(lines[2], "uuid,data,string,u1234567890123456")
        self.assertEqual(len(lines), 5)


@unittest.skipUnless(idf_env_available(), "ESP-IDF not found")
class TestWriteFlow(unittest.TestCase):
    """Device flow with parttool mocked; generation + parsing are real."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tuya_auth_wf_")
        self.addCleanup(self.tmp.cleanup)
        os.environ["IDF_PATH"] = IDF_PATH

        self.txt = os.path.join(self.tmp.name, "tuya_authkey.txt")
        with open(self.txt, "w", encoding="utf-8") as f:
            f.write("TUYA_UUID=uuid-writeflow-1234\n"
                    "TUYA_AUTH_KEY=" + "k" * 32 + "\n"
                    "TUYA_PRODUCT_KEY=pkwriteflow12345\n")

        self.parttool_calls = []

        def fake_run_parttool(_parttool, port, baud, extra, capture=True):
            self.parttool_calls.append(extra)
            if extra[0] == "get_partition_info":
                res = mock.Mock()
                res.stdout = "0x4000\n"
                return res
            if extra[0] == "read_partition":
                # Simulate the device by regenerating an image from the same
                # CSV the write path produced (found via write_partition call).
                import subprocess as sp
                csv_candidate = None
                for call in self.parttool_calls:
                    if call[0] == "write_partition":
                        csv_candidate = call[call.index("--input") + 1]
                csv_candidate = csv_candidate[:-4] + ".csv"
                sp.run([sys.executable, "-m", "esp_idf_nvs_partition_gen",
                        "generate", csv_candidate, extra[extra.index("--output") + 1],
                        "0x4000"],
                       check=True, capture_output=True)
            return mock.Mock()

        self.fake_run_parttool = fake_run_parttool

    def test_write_reports_and_verifies(self):
        args = types.SimpleNamespace(
            port="/dev/fake", baud=None, file=self.txt, uuid=None,
            auth_key=None, pid=None, no_verify=False)
        with mock.patch.object(tuya_auth_tool, "run_parttool",
                               self.fake_run_parttool), \
             mock.patch.object(tuya_auth_tool, "find_idf_tools",
                               return_value="parttool.py"), \
             mock.patch("builtins.print"):
            tuya_auth_tool.cmd_write(args)  # must not raise

        actions = [call[0] for call in self.parttool_calls]
        self.assertEqual(actions,
                         ["get_partition_info", "write_partition", "read_partition"])

    def test_write_detects_verification_mismatch(self):
        def corrupt_run_parttool(_parttool, port, baud, extra, capture=True):
            self.parttool_calls.append(extra)
            if extra[0] == "get_partition_info":
                res = mock.Mock()
                res.stdout = "0x4000\n"
                return res
            if extra[0] == "read_partition":
                values = tuya_auth_tool.parse_authkey_file(self.txt)
                values["uuid"] = "DIFFERENT-uuid-12"  # simulate stale flash
                csv_path = os.path.join(self.tmp.name, "other.csv")
                bin_path = extra[extra.index("--output") + 1]
                with open(csv_path, "w", encoding="utf-8") as f:
                    f.write(tuya_auth_tool.values_to_nvs_csv(values))
                import subprocess as sp
                sp.run([sys.executable, "-m", "esp_idf_nvs_partition_gen",
                        "generate", csv_path, bin_path, "0x4000"],
                       check=True, capture_output=True)
            return mock.Mock()

        args = types.SimpleNamespace(
            port="/dev/fake", baud=None, file=self.txt, uuid=None,
            auth_key=None, pid=None, no_verify=False)
        with mock.patch.object(tuya_auth_tool, "run_parttool", corrupt_run_parttool), \
             mock.patch.object(tuya_auth_tool, "find_idf_tools",
                               return_value="parttool.py"), \
             mock.patch("builtins.print"):
            with self.assertRaises(SystemExit) as ctx:
                tuya_auth_tool.cmd_write(args)
        self.assertIn("verification FAILED", str(ctx.exception))

    def test_write_no_verify_skips_readback(self):
        args = types.SimpleNamespace(
            port="/dev/fake", baud=None, file=self.txt, uuid=None,
            auth_key=None, pid=None, no_verify=True)
        with mock.patch.object(tuya_auth_tool, "run_parttool",
                               self.fake_run_parttool), \
             mock.patch.object(tuya_auth_tool, "find_idf_tools",
                               return_value="parttool.py"), \
             mock.patch("builtins.print"):
            tuya_auth_tool.cmd_write(args)
        actions = [call[0] for call in self.parttool_calls]
        self.assertEqual(actions, ["get_partition_info", "write_partition"])


@unittest.skipUnless(idf_env_available(), "ESP-IDF not found")
class TestReadFlow(unittest.TestCase):
    def test_read_prints_stored_values(self):
        os.environ["IDF_PATH"] = IDF_PATH
        tmp = tempfile.TemporaryDirectory(prefix="tuya_auth_rd_")
        self.addCleanup(tmp.cleanup)

        values = {"uuid": "uuid-readflow-12345", "auth_key": "k" * 32,
                  "product_key": "pkreadflow123456"}
        csv_path = os.path.join(tmp.name, "t.csv")
        bin_path = os.path.join(tmp.name, "t.bin")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write(tuya_auth_tool.values_to_nvs_csv(values))
        import subprocess as sp
        sp.run([sys.executable, "-m", "esp_idf_nvs_partition_gen",
                "generate", csv_path, bin_path, "0x4000"],
               check=True, capture_output=True)

        captured = []

        def fake_run_parttool(_parttool, port, baud, extra, capture=True):
            res = mock.Mock()
            if extra[0] == "read_partition":
                import shutil
                shutil.copyfile(bin_path, extra[extra.index("--output") + 1])
            return res

        with mock.patch.object(tuya_auth_tool, "run_parttool", fake_run_parttool), \
             mock.patch.object(tuya_auth_tool, "find_idf_tools",
                               return_value="parttool.py"), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: captured.append(a)):
            tuya_auth_tool.cmd_read(types.SimpleNamespace(
                port="/dev/fake", baud=None))

        out = "\n".join(str(a[0]) for a in captured)
        self.assertIn("TUYA_UUID=uuid-readflow-12345", out)
        self.assertIn("TUYA_AUTH_KEY=" + "k" * 32, out)
        self.assertIn("TUYA_PRODUCT_KEY=pkreadflow123456", out)


if __name__ == "__main__":
    unittest.main()
