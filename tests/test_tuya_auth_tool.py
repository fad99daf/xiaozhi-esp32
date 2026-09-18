"""Host tests for scripts/tuya_auth_tool.py.

Covers: authkey file parsing, validation, CSV generation, and a full
generate->parse round trip using the real IDF nvs tools (no device needed).
The flash/read device paths are exercised through mocked subprocess/parttool.
"""

import csv
import importlib.util
import io
import os
import signal
import subprocess
import sys
import threading
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

    def generate_and_parse(self, values, strict=False):
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
        return tuya_auth_tool.parse_nvs_dump(bin_path, strict=strict)

    def test_round_trip_preserves_all_fields(self):
        values = {
            "uuid": "uuid-roundtrip-123",
            "auth_key": "x" * 32,
            "product_key": "pkroundtrip12345",
        }
        stored = self.generate_and_parse(values)[tuya_auth_tool.NAMESPACE]
        for key, expected in values.items():
            self.assertEqual(stored[key], expected)

    def test_strict_round_trip_preserves_stored_nuls(self):
        for suffix in ("", "\x00", "\x00extra"):
            with self.subTest(suffix=suffix):
                values = {"uuid": "synthetic-uuid-01" + suffix,
                          "auth_key": "k" * 32, "product_key": "p" * 16}
                stored = self.generate_and_parse(values, strict=True)
                self.assertEqual(stored[tuya_auth_tool.NAMESPACE], values)

    def test_round_trip_special_chars(self):
        values = {
            "uuid": "uuid-with.dots-and_underscore",
            "auth_key": "B64chars+/=aes" + "0" * 18,
            "product_key": "pk_1234567890abc",
        }
        stored = self.generate_and_parse(values)[tuya_auth_tool.NAMESPACE]
        for key, expected in values.items():
            self.assertEqual(stored[key], expected)


@unittest.skipUnless(idf_env_available(), "ESP-IDF not found")
class TestStructuredRealRoundTrip(unittest.TestCase):
    def test_generated_image_readback_and_corruption(self):
        values = {"uuid": "synthetic-uuid-01", "auth_key": "k" * 32,
                  "product_key": "synthetic-pid-001"}
        for corruption in (None, "page_crc", "entry_crc", "truncation"):
            with self.subTest(corruption=corruption), \
                 tempfile.TemporaryDirectory(dir="/tmp") as parent:
                image = None
                actions = []

                def operation(parttool, port, baud, extra, stage, *args, **kwargs):
                    nonlocal image
                    actions.append(extra[0])
                    self.assertEqual(kwargs["after"], "no_reset")
                    if extra[0] == "write_partition":
                        with open(extra[extra.index("--input") + 1], "rb") as handle:
                            image = bytearray(handle.read())
                    elif extra[0] == "read_partition":
                        # NVS page header CRC is at 28; first entry CRC at 64 + 4.
                        if corruption == "page_crc":
                            image[28] ^= 1
                        elif corruption == "entry_crc":
                            image[68] ^= 1
                        elif corruption == "truncation":
                            del image[-4096:]
                        path = extra[extra.index("--output") + 1]
                        with open(path, "wb") as handle:
                            handle.write(image)
                        # These corruptions leave the logical identity intact:
                        # field-only verification cannot detect them.
                        stored = tuya_auth_tool.parse_nvs_dump(path, strict=True)
                        self.assertEqual(stored[tuya_auth_tool.NAMESPACE], values)
                    return types.SimpleNamespace(stdout="0x4000\n")

                with mock.patch.dict(os.environ, {"IDF_PATH": IDF_PATH}), \
                     mock.patch.object(tuya_auth_tool, "run_parttool_operation",
                                       side_effect=operation), \
                     mock.patch.object(tuya_auth_tool, "parse_nvs_dump",
                                       wraps=tuya_auth_tool.parse_nvs_dump) as parse:
                    if corruption is None:
                        self.assertTrue(tuya_auth_tool.write_verify_identity(
                            "/dev/synthetic", None, values, temp_parent=parent,
                            after="no_reset"))
                        self.assertEqual(parse.call_count, 2)
                    else:
                        with self.assertRaises(tuya_auth_tool.AuthToolError) as caught:
                            tuya_auth_tool.write_verify_identity(
                                "/dev/synthetic", None, values, temp_parent=parent,
                                after="no_reset")
                        self.assertEqual(caught.exception.stage, "auth_verify")
                        self.assertIsNone(caught.exception.detail)
                        for value in values.values():
                            self.assertNotIn(value, str(caught.exception))
                        self.assertEqual(parse.call_count, 1)
                self.assertEqual(actions, ["get_partition_info", "write_partition",
                                           "read_partition"])
                self.assertEqual(os.listdir(parent), [])


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


class TestStructuredValidation(unittest.TestCase):
    def setUp(self):
        self.values = {"uuid": "u" * 16, "auth_key": "k" * 32,
                       "product_key": "p" * 16}

    def assert_invalid(self, values, key, message):
        with self.assertRaises(tuya_auth_tool.AuthToolError) as caught:
            tuya_auth_tool.validate_values_structured(values)
        error = caught.exception
        self.assertEqual(error.stage, "auth_write")
        self.assertIn(key, error.message)
        self.assertIn(message, error.message)
        self.assertIsNone(error.detail)
        for value in values.values():
            if isinstance(value, str) and len(value) > 3:
                self.assertNotIn(value, str(error))

    def test_ascii_boundaries_for_each_field(self):
        for key, (low, high) in {"uuid": (16, 31), "auth_key": (32, 63),
                                 "product_key": (16, 31)}.items():
            for length in (low - 1, low, high, high + 1):
                with self.subTest(key=key, length=length):
                    values = dict(self.values, **{key: "x" * length})
                    original = dict(values)
                    if low <= length <= high:
                        self.assertIsNone(tuya_auth_tool.validate_values_structured(values))
                    else:
                        self.assert_invalid(values, key, "UTF-8 length %d" % length)
                    self.assertEqual(values, original)

    def test_utf8_limits_count_bytes_not_characters(self):
        for key, (low, high) in {"uuid": (16, 31), "auth_key": (32, 63),
                                 "product_key": (16, 31)}.items():
            for length in (low - 1, low, high, high + 1):
                with self.subTest(key=key, bytes=length):
                    value = "\u00e9" * (length // 2) + "x" * (length % 2)
                    self.assertEqual(len(value.encode("utf-8")), length)
                    values = dict(self.values, **{key: value})
                    if low <= length <= high:
                        tuya_auth_tool.validate_values_structured(values)
                    else:
                        self.assert_invalid(values, key, "UTF-8 length %d" % length)

    def test_missing_empty_and_non_string_fields(self):
        for key in self.values:
            with self.subTest(key=key, missing=True):
                values = dict(self.values)
                del values[key]
                self.assert_invalid(values, key, "missing credential field")
            for value in (None, "", 123, False, b"x" * 32, [], {}):
                with self.subTest(key=key, value=value):
                    self.assert_invalid(dict(self.values, **{key: value}), key,
                                        "missing credential field")

    def test_nul_rejected_at_every_position_in_each_field(self):
        for key, value in self.values.items():
            for position in (0, len(value) // 2, len(value) - 1):
                with self.subTest(key=key, position=position):
                    corrupted = value[:position] + "\x00" + value[position + 1:]
                    self.assert_invalid(dict(self.values, **{key: corrupted}),
                                        key, "contains NUL")

    def test_structured_errors_and_cancellation_metadata(self):
        error = tuya_auth_tool.AuthToolError("auth_read", "read failed", "detail")
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(str(error), "read failed: detail")
        self.assertEqual((error.stage, error.message, error.detail),
                         ("auth_read", "read failed", "detail"))
        self.assertEqual(str(tuya_auth_tool.AuthToolError("x", "plain")), "plain")
        self.assertEqual(tuya_auth_tool.CommandCancelled().stage, "cancelled")
        event = threading.Event()
        tuya_auth_tool.check_cancelled(None, "auth_read")
        tuya_auth_tool.check_cancelled(event, "auth_read")
        event.set()
        with self.assertRaises(tuya_auth_tool.CommandCancelled) as caught:
            tuya_auth_tool.check_cancelled(event, "auth_read")
        self.assertEqual(caught.exception.stage, "auth_read")
        self.assertEqual(str(caught.exception), "operation cancelled")


class TestStructuredCommand(unittest.TestCase):
    def setUp(self):
        self.command = ["fake-tool", "--option", "value"]
        self.process = mock.Mock(pid=43210, returncode=0)
        self.process.poll.return_value = None
        self.process.communicate.return_value = ("output", "diagnostic")
        self.popen = self.enter_patch("subprocess.Popen", return_value=self.process)
        self.killpg = self.enter_patch("os.killpg")
        self.enter_patch("os.name", "posix")
        self.clock = self.enter_patch("time.monotonic", return_value=100.0)

    def enter_patch(self, name, *args, **kwargs):
        patcher = mock.patch("tuya_auth_tool." + name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def test_success_captures_output_environment_and_isolates_process_group(self):
        env = {"SYNTHETIC_ENV": "test"}
        result = tuya_auth_tool.run_command(self.command, "auth_write", env=env)
        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertEqual((result.args, result.returncode, result.stdout, result.stderr),
                         (self.command, 0, "output", "diagnostic"))
        self.popen.assert_called_once_with(
            self.command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, start_new_session=True)
        self.process.communicate.assert_called_once_with(timeout=0.2)
        self.killpg.assert_not_called()
        self.process.wait.assert_not_called()

    def test_communicate_polls_until_completion_without_sleep(self):
        self.process.communicate.side_effect = [
            subprocess.TimeoutExpired(self.command, 0.2), ("done", "")]
        result = tuya_auth_tool.run_command(self.command, "auth_read")
        self.assertEqual(result.stdout, "done")
        self.assertEqual(self.process.communicate.call_args_list,
                         [mock.call(timeout=0.2), mock.call(timeout=0.2)])
        self.killpg.assert_not_called()

    def test_communicate_timeout_is_clamped_to_remaining_budget(self):
        self.clock.side_effect = [10.0, 10.875]
        tuya_auth_tool.run_command(self.command, "auth_read", timeout=1)
        self.process.communicate.assert_called_once_with(timeout=0.125)

    def test_start_failure_has_structured_stage_and_cause(self):
        failure = OSError("synthetic executable missing")
        self.popen.side_effect = failure
        with self.assertRaises(tuya_auth_tool.AuthToolError) as caught:
            tuya_auth_tool.run_command(self.command, "auth_write")
        self.assertEqual(caught.exception.stage, "auth_write")
        self.assertEqual(caught.exception.message, "could not start command")
        self.assertEqual(caught.exception.detail, str(failure))
        self.assertIs(caught.exception.__cause__, failure)
        self.killpg.assert_not_called()

    def test_nonzero_exit_selects_fatal_lines_or_last_five_diagnostics(self):
        cases = [
            ("noise\n Fatal Error: first \nFATAL ERROR: second\n", "ignored",
             "Fatal Error: first | FATAL ERROR: second"),
            ("ignored", "0\n1\n2\n3\n4\n5\n6\n", "2 | 3 | 4 | 5 | 6"),
            ("stdout only\n", "", "stdout only"),
            ("", "", "command failed"),
        ]
        self.process.returncode = 7
        for stdout, stderr, detail in cases:
            with self.subTest(stdout=stdout, stderr=stderr):
                self.process.communicate.return_value = (stdout, stderr)
                with self.assertRaises(tuya_auth_tool.AuthToolError) as caught:
                    tuya_auth_tool.run_command(self.command, "auth_read")
                self.assertEqual(caught.exception.stage, "auth_read")
                self.assertEqual(caught.exception.message, "command failed")
                self.assertEqual(caught.exception.detail, detail)
        self.killpg.assert_not_called()

    def test_pre_cancel_does_not_spawn(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(tuya_auth_tool.CommandCancelled) as caught:
            tuya_auth_tool.run_command(self.command, "auth_write", cancel_event=event)
        self.assertEqual(caught.exception.stage, "auth_write")
        self.popen.assert_not_called()
        self.killpg.assert_not_called()

    def test_cancellation_after_poll_terminates_group_and_reaps(self):
        event = threading.Event()

        def pending(**kwargs):
            event.set()
            raise subprocess.TimeoutExpired(self.command, kwargs["timeout"])

        self.process.communicate.side_effect = pending
        with self.assertRaises(tuya_auth_tool.CommandCancelled) as caught:
            tuya_auth_tool.run_command(self.command, "auth_read", cancel_event=event)
        self.assertEqual(caught.exception.stage, "auth_read")
        self.assertEqual(self.killpg.call_args_list, [
            mock.call(43210, signal.SIGTERM), mock.call(43210, signal.SIGKILL)])
        self.assertEqual(self.process.wait.call_args_list, [mock.call(timeout=2), mock.call()])
        self.process.communicate.assert_called_once_with(timeout=0.2)

    def test_timeout_escalates_group_kill_and_reaps_in_order(self):
        self.clock.side_effect = [10.0, 10.0, 11.0]
        self.process.communicate.side_effect = subprocess.TimeoutExpired(self.command, 0.2)
        self.process.wait.side_effect = [subprocess.TimeoutExpired(self.command, 2), None]
        order = mock.Mock()
        order.attach_mock(self.killpg, "killpg")
        order.attach_mock(self.process.wait, "wait")
        with self.assertRaises(tuya_auth_tool.AuthToolError) as caught:
            tuya_auth_tool.run_command(self.command, "auth_write", timeout=1)
        self.assertNotIsInstance(caught.exception, tuya_auth_tool.CommandCancelled)
        self.assertEqual(caught.exception.stage, "auth_write")
        self.assertEqual(str(caught.exception), "command timed out after 1.0s")
        self.assertEqual(order.mock_calls, [
            mock.call.killpg(43210, signal.SIGTERM), mock.call.wait(timeout=2),
            mock.call.killpg(43210, signal.SIGKILL), mock.call.wait()])

    def test_zero_timeout_terminates_before_communicate(self):
        with self.assertRaises(tuya_auth_tool.AuthToolError):
            tuya_auth_tool.run_command(self.command, "auth_write", timeout=0)
        self.process.communicate.assert_not_called()
        self.assertEqual(self.killpg.call_args_list, [
            mock.call(43210, signal.SIGTERM), mock.call(43210, signal.SIGKILL)])
        self.assertEqual(self.process.wait.call_args_list, [mock.call(timeout=2), mock.call()])

    def test_communication_exception_terminates_and_preserves_exception(self):
        for failure in (RuntimeError("pipe failure"), KeyboardInterrupt()):
            with self.subTest(error=type(failure).__name__):
                self.killpg.reset_mock()
                self.process.wait.reset_mock()
                self.process.communicate.side_effect = failure
                with self.assertRaises(type(failure)) as caught:
                    tuya_auth_tool.run_command(self.command, "auth_read")
                self.assertIs(caught.exception, failure)
                self.assertEqual(self.killpg.call_args_list, [
                    mock.call(43210, signal.SIGTERM), mock.call(43210, signal.SIGKILL)])
                self.assertEqual(self.process.wait.call_args_list,
                                 [mock.call(timeout=2), mock.call()])

    def test_already_exited_leader_still_has_its_group_cleaned_up(self):
        self.process.poll.return_value = 0
        with self.assertRaises(tuya_auth_tool.AuthToolError):
            tuya_auth_tool.run_command(self.command, "auth_write", timeout=0)
        self.assertEqual(self.killpg.call_args_list, [
            mock.call(43210, signal.SIGTERM), mock.call(43210, signal.SIGKILL)])
        self.process.wait.assert_any_call()

    def test_process_disappearing_during_term_preserves_timeout(self):
        self.killpg.side_effect = ProcessLookupError()
        with self.assertRaises(tuya_auth_tool.AuthToolError) as caught:
            tuya_auth_tool.run_command(self.command, "auth_read", timeout=0)
        self.assertIn("timed out", str(caught.exception))
        self.assertEqual(self.killpg.call_args_list, [
            mock.call(43210, signal.SIGTERM), mock.call(43210, signal.SIGKILL)])
        self.process.wait.assert_any_call()

    def test_process_disappearing_during_kill_is_still_reaped(self):
        self.killpg.side_effect = [None, ProcessLookupError()]
        self.process.wait.side_effect = [subprocess.TimeoutExpired(self.command, 2), None]
        with self.assertRaises(tuya_auth_tool.AuthToolError) as caught:
            tuya_auth_tool.run_command(self.command, "auth_read", timeout=0)
        self.assertIn("timed out", str(caught.exception))
        self.assertEqual(self.killpg.call_args_list, [
            mock.call(43210, signal.SIGTERM), mock.call(43210, signal.SIGKILL)])
        self.assertEqual(self.process.wait.call_args_list,
                         [mock.call(timeout=2), mock.call()])

    def test_non_posix_uses_process_terminate_and_kill(self):
        self.process.wait.side_effect = [subprocess.TimeoutExpired(self.command, 2), None]
        with mock.patch.object(tuya_auth_tool.os, "name", "nt"):
            with self.assertRaises(tuya_auth_tool.AuthToolError):
                tuya_auth_tool.run_command(self.command, "auth_write", timeout=0)
        self.assertNotIn("start_new_session", self.popen.call_args.kwargs)
        self.process.terminate.assert_called_once_with()
        self.process.kill.assert_called_once_with()
        self.assertEqual(self.process.wait.call_args_list,
                         [mock.call(timeout=2), mock.call()])
        self.killpg.assert_not_called()


@unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
class TestRealProcessCancellation(unittest.TestCase):
    def test_real_command_timeout_and_cancellation_reap_and_close_pipes(self):
        original_popen = subprocess.Popen
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                event = threading.Event()
                processes = []

                def spawn(*args, **kwargs):
                    process = original_popen(*args, **kwargs)
                    processes.append(process)
                    if cancelled:
                        event.set()
                    return process

                error = (tuya_auth_tool.CommandCancelled if cancelled
                         else tuya_auth_tool.AuthToolError)
                with mock.patch.object(tuya_auth_tool.subprocess, "Popen", side_effect=spawn):
                    with self.assertRaises(error):
                        tuya_auth_tool.run_command(
                            [sys.executable, "-c", "import time; time.sleep(30)"],
                            "auth_read", timeout=0.05, cancel_event=event)
                process = processes[0]
                self.assertIsNotNone(process.returncode)
                self.assertTrue(process.stdout.closed)
                self.assertTrue(process.stderr.closed)
                with self.assertRaises(ProcessLookupError):
                    os.kill(process.pid, 0)

    def test_cleanup_kills_grandchild_even_when_leader_exits(self):
        child = (
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); time.sleep(30)"
        )
        for exit_first in (False, True):
            with self.subTest(exit_first=exit_first):
                leader = (
                    "import subprocess,sys,time; "
                    "subprocess.Popen([sys.executable, '-c', sys.argv[1]]); "
                    + ("time.sleep(30)" if not exit_first else "")
                )
                process = subprocess.Popen(
                    [sys.executable, "-c", leader, child], start_new_session=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    self.assertEqual(process.stdout.readline().strip(), "ready")
                    if exit_first:
                        process.wait(timeout=5)
                    tuya_auth_tool._terminate_process(process)
                    # The grandchild inherits both pipes: EOF proves it stopped,
                    # even on hosts where an orphan zombie is reaped later.
                    process.communicate(timeout=3)
                    self.assertIsNotNone(process.returncode)
                finally:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate(timeout=5)


class TestStructuredParttool(unittest.TestCase):
    def test_parttool_arguments_and_control_options(self):
        for baud, offset in ((baud, offset) for baud in (None, 0, 460800)
                             for offset in (None, 0, 0xC000)):
            with self.subTest(baud=baud, offset=offset):
                extra = ["read_partition", "--partition-name", "nvs", "--output", "dump.bin"]
                event = threading.Event()
                with mock.patch.object(tuya_auth_tool, "run_command") as run:
                    result = tuya_auth_tool.run_parttool_operation(
                        "fake parttool.py", "/dev/fake port", baud, extra,
                        "auth_read", timeout=8, cancel_event=event,
                        partition_table_offset=offset)
                expected = [sys.executable, "fake parttool.py", "--port",
                            "/dev/fake port", "--quiet"]
                if baud:
                    expected += ["--baud", str(baud)]
                if offset is not None:
                    expected += ["--partition-table-offset", hex(offset)]
                run.assert_called_once_with(expected + extra, "auth_read",
                                            timeout=8, cancel_event=event)
                self.assertIs(result, run.return_value)
                self.assertEqual(extra, ["read_partition", "--partition-name", "nvs",
                                         "--output", "dump.bin"])

    @unittest.skipUnless(idf_env_available(), "ESP-IDF not found")
    def test_reset_and_custom_offset_reach_installed_parttool_subprocesses(self):
        tool_dir = os.path.join(IDF_PATH, "components", "partition_table")
        path = os.path.join(tool_dir, "parttool.py")
        spec = importlib.util.spec_from_file_location("synthetic_parttool", path)
        parttool = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "path", [tool_dir] + sys.path):
            spec.loader.exec_module(parttool)
        partition = types.SimpleNamespace(
            name="nvs", type=1, subtype=2, offset=0x9000, size=0x4000,
            readonly=False, encrypted=False)
        table = mock.Mock()
        table.find_by_name.return_value = partition

        def execute(command, stage, **kwargs):
            with mock.patch.object(sys, "argv", command[1:]):
                parttool.main()
            return types.SimpleNamespace(stdout="0x4000\n")

        for operation, options, expected in (
                ("get_partition_info", ["--info", "size"], ["read_flash"]),
                ("write_partition", ["--input", "synthetic.bin"],
                 ["read_flash", "erase_region", "write_flash"]),
                ("read_partition", ["--output", "synthetic.bin"],
                 ["read_flash", "read_flash"])):
            with self.subTest(operation=operation), \
                 mock.patch.object(tuya_auth_tool, "run_command", side_effect=execute), \
                 mock.patch.object(parttool.gen.PartitionTable, "from_binary", return_value=table), \
                 mock.patch.object(parttool.subprocess, "check_call") as check_call, \
                 mock.patch("builtins.open", mock.mock_open(read_data=b"synthetic")), \
                 mock.patch("builtins.print"):
                tuya_auth_tool.run_parttool_operation(
                    path, "/dev/synthetic", 115200,
                    [operation, "--partition-name", "nvs"] + options,
                    "auth_write", before="usb_reset", after="no_reset",
                    partition_table_offset=0xC000)
                commands = [call.args[0] for call in check_call.call_args_list]
                self.assertEqual(len(commands), len(expected))
                read_index = commands[0].index("read_flash")
                self.assertEqual(commands[0][read_index + 1], str(0xC000))
                for command, action in zip(commands, expected):
                    self.assertEqual(command[2:6],
                                     ["--before", "usb_reset", "--after", "no_reset"])
                    self.assertIn(action, command)

    def test_partition_size_uses_final_line_and_accepts_positive_base_zero_values(self):
        event = threading.Event()
        for output, expected in (("0x4000\n", 16384), ("banner\n 16384 \n", 16384),
                                 ("0X8000", 32768), ("1", 1)):
            with self.subTest(output=output):
                with mock.patch.object(tuya_auth_tool, "run_parttool_operation",
                                       return_value=types.SimpleNamespace(stdout=output)) as run:
                    size = tuya_auth_tool.get_partition_size_operation(
                        "fake.py", "/dev/fake", 115200, timeout=9, cancel_event=event)
                self.assertEqual(size, expected)
                run.assert_called_once_with(
                    "fake.py", "/dev/fake", 115200,
                    ["get_partition_info", "--partition-name", "nvs", "--info", "size"],
                    "auth_write", 9, event, before=None, after=None, partition_table_offset=None)

    def test_partition_size_rejects_empty_malformed_zero_and_negative(self):
        for output in ("", " \n", "banner", "0x4000\ntrailing noise", "0", "-1", "-0x1000"):
            with self.subTest(output=output):
                with mock.patch.object(tuya_auth_tool, "run_parttool_operation",
                                       return_value=types.SimpleNamespace(stdout=output)):
                    with self.assertRaises(tuya_auth_tool.AuthToolError) as caught:
                        tuya_auth_tool.get_partition_size_operation("fake.py", "/dev/fake", None)
                self.assertEqual(caught.exception.stage, "auth_write")
                expected = "invalid" if output in ("0", "-1", "-0x1000") else "could not parse"
                self.assertEqual(str(caught.exception), expected + " nvs partition size")

    def test_partition_size_propagates_failure_and_cancellation(self):
        for error in (tuya_auth_tool.AuthToolError("auth_write", "size failed"),
                      tuya_auth_tool.CommandCancelled("auth_write")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(tuya_auth_tool, "run_parttool_operation", side_effect=error):
                    with self.assertRaises(type(error)) as caught:
                        tuya_auth_tool.get_partition_size_operation("fake.py", "/dev/fake", None)
                self.assertIs(caught.exception, error)

    def test_structured_tool_discovery(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(tuya_auth_tool.AuthToolError) as caught:
                tuya_auth_tool.find_idf_tools_structured()
        self.assertEqual(caught.exception.stage, "preflight")
        self.assertEqual(caught.exception.message, "IDF_PATH is not set")
        with mock.patch.dict(os.environ, {"IDF_PATH": "/synthetic/idf"}):
            for exists in (False, True):
                with self.subTest(exists=exists):
                    with mock.patch.object(tuya_auth_tool.os.path, "isfile", return_value=exists) as isfile:
                        if exists:
                            self.assertEqual(tuya_auth_tool.find_idf_tools_structured(),
                                             "/synthetic/idf/components/partition_table/parttool.py")
                        else:
                            with self.assertRaises(tuya_auth_tool.AuthToolError) as caught:
                                tuya_auth_tool.find_idf_tools_structured()
                            self.assertEqual(caught.exception.stage, "preflight")
                            self.assertEqual(caught.exception.message, "parttool.py not found")
                    isfile.assert_called_once_with(
                        "/synthetic/idf/components/partition_table/parttool.py")


class TestStructuredNvsParsing(unittest.TestCase):
    def parse_synthetic(self, field, raw, strict=True, *, size=None, padding=b"padding ignored"):
        namespace = types.SimpleNamespace(
            state="Written", metadata={"namespace": 0},
            data={"value": 1}, key="tuya_auth")
        entry = types.SimpleNamespace(
            state="Written", metadata={"namespace": 1, "type": "string"},
            data={"size": len(raw) if size is None else size}, key=field,
            children=[types.SimpleNamespace(raw=raw + padding)])
        parser = types.ModuleType("nvs_parser")
        parser.NVS_Partition = mock.Mock(return_value=types.SimpleNamespace(
            pages=[types.SimpleNamespace(entries=[namespace, entry])]))
        with mock.patch.dict(sys.modules, {"nvs_parser": parser}), \
             mock.patch.dict(os.environ, {"IDF_PATH": "/synthetic/idf"}), \
             mock.patch.object(sys, "path", list(sys.path)), \
             mock.patch("builtins.open", mock.mock_open(read_data=b"synthetic image")) as opened:
            result = tuya_auth_tool.parse_nvs_dump("/synthetic/dump.bin", strict=strict)
        opened.assert_called_once_with("/synthetic/dump.bin", "rb")
        parser.NVS_Partition.assert_called_once_with("nvs", bytearray(b"synthetic image"))
        return result

    def test_strict_utf8_decode_removes_only_the_stored_terminator(self):
        value = "synthetic-\u00e9-value"
        for field in ("uuid", "auth_key", "product_key"):
            with self.subTest(field=field):
                result = self.parse_synthetic(
                    field, value.encode("utf-8") + b"\x00", padding=b"\x00" * 32)
                self.assertEqual(result["tuya_auth"], {field: value})

    def test_strict_decode_preserves_embedded_and_extra_nuls_for_exact_comparison(self):
        for raw in (b"synthetic\x00value\x00", b"synthetic-value\x00\x00"):
            with self.subTest(raw=raw):
                result = self.parse_synthetic("uuid", raw)
                self.assertEqual(result["tuya_auth"]["uuid"], raw[:-1].decode("utf-8"))

    def test_strict_decode_rejects_empty_truncated_and_unterminated_payloads(self):
        for raw, size in ((b"", 0), (b"value", 5), (b"value\x00", 7),
                          (b"value\x00", -1)):
            with self.subTest(raw=raw, size=size):
                with self.assertRaisesRegex(ValueError, "malformed NVS string payload"):
                    self.parse_synthetic("uuid", raw, size=size, padding=b"")

    def test_strict_decode_rejects_invalid_utf8_in_each_field(self):
        for field in ("uuid", "auth_key", "product_key"):
            with self.subTest(field=field):
                with self.assertRaises(UnicodeDecodeError):
                    self.parse_synthetic(field, b"synthetic-\xff\x00")

    def test_non_strict_decode_retains_legacy_replacement_behavior(self):
        result = self.parse_synthetic("uuid", b"synthetic-\xff\x00", strict=False)
        self.assertEqual(result["tuya_auth"]["uuid"], "synthetic-\ufffd")


class TestStructuredWriteVerify(unittest.TestCase):
    def setUp(self):
        self.values = {"uuid": "synthetic-uuid-01", "auth_key": "synthetic-auth-key-" + "x" * 14,
                       "product_key": "synthetic-pid-01x"}
        self.event = threading.Event()
        self.timeline = []
        self.cancel_at = None
        self.failure_at = None
        self.failure = None
        self.stored = {tuya_auth_tool.NAMESPACE: dict(self.values)}
        self.tmp_path = "/tmp/synthetic-tuya-auth"
        self.csv_path = self.tmp_path + "/tuya_auth.csv"
        self.image_path = self.tmp_path + "/tuya_auth.bin"
        self.dump_path = self.tmp_path + "/nvs_dump.bin"
        self.temp = mock.MagicMock()
        self.temp.__enter__.return_value = self.tmp_path
        self.temp.__exit__.return_value = False
        self.temporary_directory = self.patch("tempfile.TemporaryDirectory", return_value=self.temp)
        self.chmod = self.patch("os.chmod")
        self.open_fd = self.patch("os.open", return_value=123)
        self.fdopen = self.patch("os.fdopen")
        self.csv_file = self.fdopen.return_value.__enter__.return_value
        image_open = mock.patch("builtins.open", side_effect=lambda *args, **kwargs:
                                io.BytesIO(b"synthetic NVS"))
        self.image_open = image_open.start()
        self.addCleanup(image_open.stop)
        self.find_tools = self.patch("find_idf_tools_structured", side_effect=self.find)
        self.run = self.patch("run_command", side_effect=self.command)
        self.parttool = self.patch("run_parttool_operation", side_effect=self.operation)
        self.real_parse = tuya_auth_tool.parse_nvs_dump
        self.parse = self.patch("parse_nvs_dump", side_effect=self.parse_dump)
        self.stdout = self.patch("sys.stdout", new_callable=io.StringIO)
        self.stderr = self.patch("sys.stderr", new_callable=io.StringIO)
        self.no_process = self.patch("subprocess.Popen", side_effect=AssertionError("unexpected subprocess"))

    def patch(self, name, **kwargs):
        patcher = mock.patch("tuya_auth_tool." + name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def step(self, name):
        self.timeline.append(name)
        if self.cancel_at == name:
            self.event.set()
        if self.failure_at == name:
            raise self.failure

    def find(self):
        self.step("find")
        return "fake-parttool.py"

    def command(self, argv, stage, timeout=None, cancel_event=None):
        self.chmod.assert_any_call(self.tmp_path, 0o700)
        self.open_fd.assert_any_call(
            self.csv_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.fdopen.return_value.__exit__.assert_any_call(None, None, None)
        self.step("generate")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def operation(self, parttool, port, baud, extra, stage, timeout=None, cancel_event=None,
                  *, before=None, after=None, partition_table_offset=None):
        if extra[0] == "write_partition":
            self.chmod.assert_any_call(self.image_path, 0o600)
        self.step(extra[0])
        return types.SimpleNamespace(stdout="0x4000\n")

    def parse_dump(self, path, strict=False):
        self.step("parse")
        return self.stored

    def stage(self, name):
        self.step(name)

    def invoke(self):
        return tuya_auth_tool.write_verify_identity(
            "/dev/fake", 460800, self.values, timeout=12,
            cancel_event=self.event, stage_callback=self.stage, temp_parent="/tmp")

    def assert_private_output(self, error=None):
        public = self.stdout.getvalue() + self.stderr.getvalue()
        public += repr(self.run.call_args_list) + repr(self.parttool.call_args_list)
        public += repr(self.timeline)
        if error is not None:
            public += str(error) + repr(error.args)
            if isinstance(error, tuya_auth_tool.AuthToolError):
                public += repr((error.stage, error.message, error.detail))
        for value in self.values.values():
            self.assertNotIn(value, public)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertEqual(self.stderr.getvalue(), "")
        self.no_process.assert_not_called()

    def assert_temp_closed(self, error=None):
        self.temporary_directory.assert_called_once_with(prefix="tuya_auth_", dir="/tmp")
        self.temp.__enter__.assert_called_once_with()
        self.temp.__exit__.assert_called_once()
        if error is None:
            self.temp.__exit__.assert_called_once_with(None, None, None)
        else:
            args = self.temp.__exit__.call_args.args
            self.assertIs(args[0], type(error))
            self.assertIs(args[1], error)
            self.assertIsNotNone(args[2])
        self.assert_private_output(error)

    def test_success_stage_order_exact_arguments_and_private_files(self):
        original = dict(self.values)
        self.assertIs(self.invoke(), True)
        self.assertEqual(self.values, original)
        self.assertEqual(self.timeline, [
            "find", "auth_write", "get_partition_info", "generate", "write_partition",
            "auth_read", "read_partition", "auth_verify", "parse"])
        self.run.assert_called_once_with(
            [sys.executable, "-m", "esp_idf_nvs_partition_gen", "generate",
             self.csv_path, self.image_path, "0x4000"],
            "auth_write", timeout=12, cancel_event=self.event)
        self.assertEqual(self.parttool.call_args_list, [
            mock.call("fake-parttool.py", "/dev/fake", 460800,
                      ["get_partition_info", "--partition-name", "nvs", "--info", "size"],
                      "auth_write", 12, self.event, before=None, after=None, partition_table_offset=None),
            mock.call("fake-parttool.py", "/dev/fake", 460800,
                      ["write_partition", "--partition-name", "nvs", "--input", self.image_path],
                      "auth_write", 12, self.event, before=None, after=None, partition_table_offset=None),
            mock.call("fake-parttool.py", "/dev/fake", 460800,
                      ["read_partition", "--partition-name", "nvs", "--output", self.dump_path],
                      "auth_read", 12, self.event, before=None, after=None, partition_table_offset=None)])
        self.parse.assert_called_once_with(self.dump_path, strict=True)
        self.assertEqual(self.chmod.call_args_list, [
            mock.call(self.tmp_path, 0o700), mock.call(self.image_path, 0o600)])
        self.open_fd.assert_called_once_with(
            self.csv_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.fdopen.assert_called_once_with(123, "w", encoding="utf-8", newline="")
        self.fdopen.return_value.__exit__.assert_called_once_with(None, None, None)
        csv_text = self.csv_file.write.call_args.args[0]
        self.assertEqual(list(csv.reader(io.StringIO(csv_text))), [
            ["key", "type", "encoding", "value"], ["tuya_auth", "namespace", "", ""],
            ["uuid", "data", "string", self.values["uuid"]],
            ["auth_key", "data", "string", self.values["auth_key"]],
            ["product_key", "data", "string", self.values["product_key"]]])
        self.assert_temp_closed()

    def test_reset_and_offset_settings_apply_to_size_write_and_read(self):
        tuya_auth_tool.write_verify_identity(
            "/dev/fake", 460800, self.values, before="usb_reset", after="no_reset",
            partition_table_offset=0xC000)
        self.assertEqual([call.args[3][0] for call in self.parttool.call_args_list],
                         ["get_partition_info", "write_partition", "read_partition"])
        for call in self.parttool.call_args_list:
            self.assertEqual(call.kwargs, {"before": "usb_reset", "after": "no_reset",
                                           "partition_table_offset": 0xC000})

    def test_optional_callback_timeout_baud_and_parent_defaults(self):
        self.assertIs(tuya_auth_tool.write_verify_identity("/dev/fake", None, self.values), True)
        self.assertEqual(self.timeline, ["find", "get_partition_info", "generate",
                                         "write_partition", "read_partition", "parse"])
        self.temporary_directory.assert_called_once_with(prefix="tuya_auth_", dir=None)
        self.assertEqual(self.run.call_args.kwargs, {"timeout": None, "cancel_event": None})
        for call in self.parttool.call_args_list:
            self.assertIsNone(call.args[2])
            self.assertEqual(call.args[-2:], (None, None))
        self.temp.__exit__.assert_called_once_with(None, None, None)
        self.assert_private_output()

    def test_csv_quotes_synthetic_special_characters_and_utf8(self):
        self.values["uuid"] = 'synthetic,"\n\u00e9abc'
        self.stored = {tuya_auth_tool.NAMESPACE: dict(self.values)}
        self.assertIs(self.invoke(), True)
        rows = list(csv.reader(io.StringIO(self.csv_file.write.call_args.args[0])))
        self.assertEqual(rows[2][3], self.values["uuid"])
        self.assert_temp_closed()

    def test_invalid_values_fail_before_discovery_or_side_effects(self):
        for key in tuple(self.values):
            with self.subTest(key=key):
                original = self.values.pop(key)
                try:
                    with self.assertRaises(tuya_auth_tool.AuthToolError) as caught:
                        self.invoke()
                    self.assertEqual(caught.exception.stage, "auth_write")
                    self.assertIn(key, str(caught.exception))
                    self.assert_private_output(caught.exception)
                finally:
                    self.values[key] = original
        self.assertEqual(self.timeline, [])
        self.find_tools.assert_not_called()
        self.temporary_directory.assert_not_called()
        self.open_fd.assert_not_called()
        self.run.assert_not_called()
        self.parttool.assert_not_called()

    def test_discovery_failure_short_circuits_before_callback(self):
        self.failure_at = "find"
        self.failure = tuya_auth_tool.AuthToolError("preflight", "tools unavailable")
        with self.assertRaises(tuya_auth_tool.AuthToolError) as caught:
            self.invoke()
        self.assertIs(caught.exception, self.failure)
        self.assertEqual(self.timeline, ["find"])
        self.temporary_directory.assert_not_called()
        self.parttool.assert_not_called()
        self.assert_private_output(caught.exception)

    def test_size_failure_does_not_create_temporary_files(self):
        self.failure_at = "get_partition_info"
        self.failure = tuya_auth_tool.AuthToolError("auth_write", "size unavailable")
        with self.assertRaises(tuya_auth_tool.AuthToolError) as caught:
            self.invoke()
        self.assertIs(caught.exception, self.failure)
        self.assertEqual(self.timeline, ["find", "auth_write", "get_partition_info"])
        self.temporary_directory.assert_not_called()
        self.run.assert_not_called()
        self.parse.assert_not_called()
        self.assert_private_output(caught.exception)

    def test_operation_failures_stop_later_stages_and_clean_up(self):
        cases = [
            ("generate", "auth_write", ["generate"]),
            ("write_partition", "auth_write", ["generate", "write_partition"]),
            ("read_partition", "auth_read", ["generate", "write_partition", "auth_read", "read_partition"]),
        ]
        for failure_at, stage, suffix in cases:
            with self.subTest(operation=failure_at):
                self.timeline.clear()
                self.temp.reset_mock()
                self.temporary_directory.reset_mock()
                self.failure_at = failure_at
                self.failure = tuya_auth_tool.AuthToolError(stage, "synthetic operation failure")
                with self.assertRaises(tuya_auth_tool.AuthToolError) as caught:
                    self.invoke()
                self.assertIs(caught.exception, self.failure)
                self.assertEqual(self.timeline, ["find", "auth_write", "get_partition_info"] + suffix)
                self.parse.assert_not_called()
                self.assert_temp_closed(caught.exception)

    def test_different_image_rejected_before_parsing_without_credentials(self):
        self.image_open.side_effect = [io.BytesIO(b"generated image"),
                                       io.BytesIO(self.values["auth_key"].encode())]
        with self.assertRaises(tuya_auth_tool.AuthToolError) as caught:
            self.invoke()
        self.assertEqual(caught.exception.stage, "auth_verify")
        self.assertEqual(str(caught.exception), "malformed nvs read-back")
        self.parse.assert_not_called()
        self.assert_temp_closed(caught.exception)

    def test_unreadable_image_reports_secret_free_verification_error(self):
        self.image_open.side_effect = OSError("synthetic unreadable image")
        with self.assertRaises(tuya_auth_tool.AuthToolError) as caught:
            self.invoke()
        self.assertEqual(caught.exception.stage, "auth_verify")
        self.assertEqual(str(caught.exception), "malformed nvs read-back")
        self.parse.assert_not_called()
        self.assert_temp_closed(caught.exception)

    def test_malformed_dump_wraps_parser_errors_without_credential_message(self):
        for failure in (ValueError(self.values["auth_key"]),
                        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
                        OSError("synthetic unreadable dump")):
            with self.subTest(error=type(failure).__name__):
                self.temp.reset_mock()
                self.temporary_directory.reset_mock()
                self.failure_at = "parse"
                self.failure = failure
                with self.assertRaises(tuya_auth_tool.AuthToolError) as caught:
                    self.invoke()
                self.assertEqual(caught.exception.stage, "auth_verify")
                self.assertEqual(str(caught.exception), "malformed nvs read-back")
                self.assertIs(caught.exception.__cause__, failure)
                self.assert_temp_closed(caught.exception)

    def test_missing_namespace_reports_all_fields_without_values(self):
        self.stored = {"unrelated": dict(self.values)}
        with self.assertRaises(tuya_auth_tool.AuthToolError) as caught:
            self.invoke()
        self.assertEqual(caught.exception.stage, "auth_verify")
        self.assertEqual(str(caught.exception),
                         "verification failed for fields: uuid, auth_key, product_key")
        self.assert_temp_closed(caught.exception)

    def test_each_field_missing_mismatched_or_wrong_type_fails_exact_verification(self):
        for key in self.values:
            for variant in ("missing", "mismatch", "empty", "nul", "bytes", "none", "number"):
                with self.subTest(key=key, variant=variant):
                    self.temp.reset_mock()
                    self.temporary_directory.reset_mock()
                    stored = dict(self.values)
                    if variant == "missing":
                        del stored[key]
                    else:
                        stored[key] = {
                            "mismatch": "DIFFERENT-synthetic-value", "empty": "",
                            "nul": self.values[key] + "\x00",
                            "bytes": self.values[key].encode("utf-8"), "none": None,
                            "number": 123,
                        }[variant]
                    self.stored = {tuya_auth_tool.NAMESPACE: stored}
                    with self.assertRaises(tuya_auth_tool.AuthToolError) as caught:
                        self.invoke()
                    self.assertEqual(caught.exception.stage, "auth_verify")
                    self.assertEqual(str(caught.exception), "verification failed for fields: " + key)
                    self.assert_temp_closed(caught.exception)

    def test_stored_extra_or_embedded_nul_fails_readback_verification(self):
        parser = TestStructuredNvsParsing()
        for key, value in self.values.items():
            for raw in (value.encode() + b"\x00\x00",
                        value[:4].encode() + b"\x00" + value[4:].encode() + b"\x00"):
                with self.subTest(key=key, raw=raw):
                    def parse_dump(path, strict=False):
                        stored = dict(self.values)
                        with mock.patch.object(tuya_auth_tool, "parse_nvs_dump", self.real_parse):
                            stored.update(parser.parse_synthetic(key, raw, strict=strict)["tuya_auth"])
                        return {tuya_auth_tool.NAMESPACE: stored}

                    self.parse.side_effect = parse_dump
                    with self.assertRaises(tuya_auth_tool.AuthToolError) as caught:
                        self.invoke()
                    self.assertEqual(caught.exception.stage, "auth_verify")
                    self.assertEqual(str(caught.exception), "verification failed for fields: " + key)

    def test_extra_namespaces_and_fields_do_not_hide_matching_identity(self):
        self.stored[tuya_auth_tool.NAMESPACE]["extra"] = "ignored"
        self.stored["other"] = {"uuid": "unrelated"}
        self.assertIs(self.invoke(), True)
        self.assert_temp_closed()

    def test_pre_cancel_does_not_access_device_or_create_files(self):
        self.event.set()
        with self.assertRaises(tuya_auth_tool.CommandCancelled) as caught:
            self.invoke()
        self.assertEqual(caught.exception.stage, "auth_write")
        self.assertEqual(self.timeline, ["find"])
        self.temporary_directory.assert_not_called()
        self.parttool.assert_not_called()
        self.run.assert_not_called()
        self.assert_private_output(caught.exception)

    def test_cancellation_between_operations_stops_next_stage_and_cleans_up(self):
        prefix = ["find", "auth_write", "get_partition_info"]
        cases = [
            ("get_partition_info", "auth_write", prefix, False),
            ("generate", "auth_write", prefix + ["generate"], True),
            ("write_partition", "auth_read", prefix + ["generate", "write_partition", "auth_read"], True),
            ("auth_read", "auth_read", prefix + ["generate", "write_partition", "auth_read"], True),
            ("read_partition", "auth_verify", prefix + ["generate", "write_partition", "auth_read",
                                                       "read_partition", "auth_verify"], True),
            ("auth_verify", "auth_verify", prefix + ["generate", "write_partition", "auth_read",
                                                     "read_partition", "auth_verify"], True),
        ]
        for cancel_at, stage, timeline, entered in cases:
            with self.subTest(cancel_at=cancel_at):
                self.timeline.clear()
                self.event.clear()
                self.temp.reset_mock()
                self.temporary_directory.reset_mock()
                self.cancel_at = cancel_at
                with self.assertRaises(tuya_auth_tool.CommandCancelled) as caught:
                    self.invoke()
                self.assertEqual(caught.exception.stage, stage)
                self.assertEqual(self.timeline, timeline)
                self.parse.assert_not_called()
                if entered:
                    self.assert_temp_closed(caught.exception)
                else:
                    self.temporary_directory.assert_not_called()
                    self.assert_private_output(caught.exception)

    def test_cancellation_from_device_operations_is_not_wrapped(self):
        for operation, stage in (("generate", "auth_write"), ("write_partition", "auth_write"),
                                 ("read_partition", "auth_read")):
            with self.subTest(operation=operation):
                self.temp.reset_mock()
                self.temporary_directory.reset_mock()
                self.failure_at = operation
                self.failure = tuya_auth_tool.CommandCancelled(stage)
                with self.assertRaises(tuya_auth_tool.CommandCancelled) as caught:
                    self.invoke()
                self.assertIs(caught.exception, self.failure)
                self.parse.assert_not_called()
                self.assert_temp_closed(caught.exception)

    def test_csv_write_failure_closes_file_and_temporary_directory(self):
        error = OSError("synthetic disk failure")
        self.csv_file.write.side_effect = error
        with self.assertRaises(OSError) as caught:
            self.invoke()
        self.assertIs(caught.exception, error)
        self.fdopen.return_value.__exit__.assert_called_once()
        self.assertIs(self.fdopen.return_value.__exit__.call_args.args[1], error)
        self.run.assert_not_called()
        self.parse.assert_not_called()
        self.assert_temp_closed(error)


class TestStructuredPrivateTemporaryFiles(unittest.TestCase):
    def test_real_private_files_removed_after_success_failure_and_cancel(self):
        values = {"uuid": "synthetic-uuid-01", "auth_key": "k" * 32,
                  "product_key": "synthetic-pid-001"}
        for outcome in ("success", "read_failure", "cancel"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory(dir="/tmp") as parent:
                event = threading.Event()
                directories = []

                def generate(argv, stage, **kwargs):
                    csv_path, image_path = argv[-3:-1]
                    directory = os.path.dirname(csv_path)
                    directories.append(directory)
                    self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
                    self.assertEqual(os.stat(csv_path).st_mode & 0o777, 0o600)
                    with open(csv_path, encoding="utf-8") as handle:
                        self.assertEqual(list(csv.reader(handle))[3][-1], values["auth_key"])
                    with open(image_path, "wb") as handle:
                        handle.write(b"synthetic NVS")
                    self.assertNotIn(values["auth_key"], repr(argv))
                    return types.SimpleNamespace(stdout="", stderr="")

                def operation(parttool, port, baud, extra, stage, timeout=None, cancel_event=None,
                              *, before=None, after=None, partition_table_offset=None):
                    if extra[0] == "write_partition":
                        image_path = extra[extra.index("--input") + 1]
                        self.assertEqual(os.stat(image_path).st_mode & 0o777, 0o600)
                        if outcome == "cancel":
                            event.set()
                    elif extra[0] == "read_partition":
                        if outcome == "read_failure":
                            raise tuya_auth_tool.AuthToolError("auth_read", "synthetic timeout")
                        with open(extra[extra.index("--output") + 1], "wb") as handle:
                            handle.write(b"synthetic NVS")
                    return types.SimpleNamespace(stdout="0x4000\n")

                with mock.patch.object(tuya_auth_tool, "find_idf_tools_structured", return_value="fake.py"), \
                     mock.patch.object(tuya_auth_tool, "run_command", side_effect=generate), \
                     mock.patch.object(tuya_auth_tool, "run_parttool_operation", side_effect=operation), \
                     mock.patch.object(tuya_auth_tool, "parse_nvs_dump",
                                       return_value={tuya_auth_tool.NAMESPACE: values}):
                    if outcome == "success":
                        self.assertTrue(tuya_auth_tool.write_verify_identity(
                            "/dev/fake", None, values, cancel_event=event, temp_parent=parent))
                    else:
                        error_type = (tuya_auth_tool.CommandCancelled if outcome == "cancel"
                                      else tuya_auth_tool.AuthToolError)
                        with self.assertRaises(error_type):
                            tuya_auth_tool.write_verify_identity(
                                "/dev/fake", None, values, cancel_event=event, temp_parent=parent)
                self.assertEqual(len(directories), 1)
                self.assertFalse(os.path.exists(directories[0]))
                self.assertEqual(os.listdir(parent), [])


if __name__ == "__main__":
    unittest.main()
