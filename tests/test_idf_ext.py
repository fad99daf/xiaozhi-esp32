"""Regression tests for the project idf.py extension callbacks."""

import builtins
import importlib.util
import pathlib
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest import mock

PROJECT_DIR = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("project_idf_ext", PROJECT_DIR / "idf_ext.py")
idf_ext = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(idf_ext)


class TestIdfExtension(unittest.TestCase):
    def setUp(self):
        self.actions = idf_ext.action_extensions({}, str(PROJECT_DIR))["actions"]
        self.global_args = types.SimpleNamespace(
            port="/dev/global",
            project_dir="/project selected",
            build_dir="/build selected",
            baud=921600,
        )
        self.ctx = types.SimpleNamespace()

    def test_flash_callback_uses_global_port(self):
        callback = self.actions["tuya-auth-flash"]["callback"]
        with mock.patch.object(idf_ext.subprocess, "run") as run:
            run.return_value = types.SimpleNamespace(returncode=0)
            callback("tuya-auth-flash", self.ctx, self.global_args)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:3], [idf_ext.sys.executable, idf_ext.TOOL_PATH, "write"])
        self.assertIn("--port", cmd)
        self.assertEqual(cmd[cmd.index("--port") + 1], "/dev/global")

    def test_flash_callback_forwards_no_verify_as_flag(self):
        callback = self.actions["tuya-auth-flash"]["callback"]
        with mock.patch.object(idf_ext.subprocess, "run") as run:
            run.return_value = types.SimpleNamespace(returncode=0)
            callback("tuya-auth-flash", self.ctx, types.SimpleNamespace(port=None),
                     port="/dev/action", no_verify=True)
        cmd = run.call_args.args[0]
        self.assertIn("--no-verify", cmd)
        self.assertNotIn(True, cmd)
        self.assertEqual(cmd[cmd.index("--port") + 1], "/dev/action")

    def test_read_callback_accepts_omitted_action_port(self):
        callback = self.actions["tuya-auth-read"]["callback"]
        with mock.patch.object(idf_ext.subprocess, "run") as run:
            run.return_value = types.SimpleNamespace(returncode=0)
            callback("tuya-auth-read", self.ctx, self.global_args)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:3], [idf_ext.sys.executable, idf_ext.TOOL_PATH, "read"])
        self.assertEqual(cmd[cmd.index("--port") + 1], "/dev/global")

    def test_auto_discovers_port_when_none_supplied(self):
        callback = self.actions["tuya-auth-read"]["callback"]
        tools = types.ModuleType("idf_py_actions.tools")
        tools.get_default_serial_port = mock.Mock(return_value="/dev/discovered")
        package = types.ModuleType("idf_py_actions")
        with mock.patch.dict("sys.modules", {
            "idf_py_actions": package,
            "idf_py_actions.tools": tools,
        }), mock.patch.object(idf_ext.subprocess, "run") as run:
            run.return_value = types.SimpleNamespace(returncode=0)
            callback("tuya-auth-read", self.ctx, types.SimpleNamespace(port=None))
        tools.get_default_serial_port.assert_called_once_with()
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--port") + 1], "/dev/discovered")

    def test_batch_actions_have_no_dependencies(self):
        self.assertEqual(self.actions["tuya-batch-setup"]["dependencies"], [])
        self.assertEqual(self.actions["tuya-batch-flash"]["dependencies"], [])
        self.assertEqual(self.actions["tuya-batch-setup"]["options"], [])

    def test_batch_device_option_uses_idf_repeated_option_syntax(self):
        options = self.actions["tuya-batch-flash"]["options"]
        device = next(option for option in options if "--device" in option["names"])
        self.assertIs(device["multiple"], True)
        self.assertNotIn("action", device)

    def test_batch_callback_forwards_global_paths_baud_and_port(self):
        callback = self.actions["tuya-batch-flash"]["callback"]
        with mock.patch.object(idf_ext.subprocess, "Popen") as run:
            run.return_value.wait.return_value = 0
            callback("tuya-batch-flash", self.ctx, self.global_args)
        self.assertEqual(run.call_args.args[0], [
            idf_ext.sys.executable,
            idf_ext.BATCH_TOOL_PATH,
            "--project-dir", "/project selected",
            "--build-dir", "/build selected",
            "--baud", "921600",
            "--port", "/dev/global",
        ])

    def test_batch_callback_forwards_multiple_devices_in_order(self):
        callback = self.actions["tuya-batch-flash"]["callback"]
        args = types.SimpleNamespace(
            port=None, project_dir="/project", build_dir="/project/out", baud=460800)
        with mock.patch.object(idf_ext.subprocess, "Popen") as run:
            run.return_value.wait.return_value = 0
            callback(
                "tuya-batch-flash", self.ctx, args,
                device=("/dev/second", "/dev/first"), pid="product-pid",
                xlsx="/data/auth info.xlsx", sheet="sheet1", list_ports=True,
                dry_run=True, jobs=2, timeout=12.5, yes=True,
                retry_uuid="uuid-to-retry")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd, [
            idf_ext.sys.executable,
            idf_ext.BATCH_TOOL_PATH,
            "--project-dir", "/project",
            "--build-dir", "/project/out",
            "--baud", "460800",
            "--pid", "product-pid",
            "--device", "/dev/second",
            "--device", "/dev/first",
            "--xlsx", "/data/auth info.xlsx",
            "--sheet", "sheet1",
            "--jobs", "2",
            "--timeout", "12.5",
            "--retry-uuid", "uuid-to-retry",
            "--list-ports", "--dry-run", "--yes",
        ])

    def test_batch_setup_uses_active_interpreter_and_verifies_imports(self):
        callback = self.actions["tuya-batch-setup"]["callback"]
        real_import = builtins.__import__
        imported = []

        def recording_import(name, *args, **kwargs):
            if name in idf_ext.BATCH_DEPENDENCY_IMPORTS:
                imported.append(name)
                return types.ModuleType(name)
            return real_import(name, *args, **kwargs)

        with mock.patch.object(idf_ext.subprocess, "run") as run, \
                mock.patch("builtins.__import__", side_effect=recording_import):
            run.return_value = types.SimpleNamespace(returncode=0)
            callback("tuya-batch-setup", self.ctx, self.global_args)
        run.assert_called_once_with([
            idf_ext.sys.executable, "-m", "pip", "install", "-r",
            idf_ext.BATCH_REQUIREMENTS_PATH,
        ])
        self.assertEqual(imported, list(idf_ext.BATCH_DEPENDENCY_IMPORTS))

    def test_batch_setup_propagates_pip_exit_status_without_importing(self):
        callback = self.actions["tuya-batch-setup"]["callback"]
        with mock.patch.object(idf_ext.subprocess, "run") as run, \
                mock.patch("builtins.__import__") as import_module:
            run.return_value = types.SimpleNamespace(returncode=7)
            with self.assertRaises(SystemExit) as raised:
                callback("tuya-batch-setup", self.ctx, self.global_args)
        self.assertEqual(raised.exception.code, 7)
        import_module.assert_not_called()

    def test_batch_setup_fails_when_dependency_is_still_missing(self):
        callback = self.actions["tuya-batch-setup"]["callback"]
        real_import = builtins.__import__

        def missing_openpyxl(name, *args, **kwargs):
            if name == "openpyxl":
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        with mock.patch.object(idf_ext.subprocess, "run") as run, \
                mock.patch("builtins.__import__", side_effect=missing_openpyxl):
            run.return_value = types.SimpleNamespace(returncode=0)
            with self.assertRaises(SystemExit) as raised:
                callback("tuya-batch-setup", self.ctx, self.global_args)
        self.assertEqual(raised.exception.code, 1)

    def test_batch_callback_propagates_backend_exit_status(self):
        callback = self.actions["tuya-batch-flash"]["callback"]
        with mock.patch.object(idf_ext.subprocess, "Popen") as run:
            run.return_value.wait.return_value = 130
            with self.assertRaises(SystemExit) as raised:
                callback("tuya-batch-flash", self.ctx, self.global_args)
        self.assertEqual(raised.exception.code, 130)

    def test_batch_callback_forwards_repeated_signals_and_restores_handlers(self):
        callback = self.actions["tuya-batch-flash"]["callback"]
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        for signum in previous:
            with self.subTest(signum=signum):
                def wait():
                    handler = signal.getsignal(signum)
                    handler(signum, None)
                    handler(signum, None)
                    return 0

                with mock.patch.object(idf_ext.subprocess, "Popen") as popen:
                    popen.return_value.wait.side_effect = wait
                    with self.assertRaises(SystemExit) as raised:
                        callback("tuya-batch-flash", self.ctx, self.global_args)
                self.assertEqual(raised.exception.code, 130)
                self.assertEqual(popen.return_value.send_signal.call_args_list,
                                 [mock.call(signum), mock.call(signum)])
                popen.return_value.kill.assert_not_called()
                for sig, handler in previous.items():
                    self.assertIs(signal.getsignal(sig), handler)

    def test_batch_callback_forwards_signal_received_during_spawn(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum):
                process = mock.Mock()
                process.wait.return_value = 0

                def spawn(*args):
                    signal.getsignal(signum)(signum, None)
                    return process

                with mock.patch.object(idf_ext.subprocess, "Popen", side_effect=spawn):
                    with self.assertRaises(SystemExit) as raised:
                        self.actions["tuya-batch-flash"]["callback"](
                            "tuya-batch-flash", self.ctx, self.global_args)
                self.assertEqual(raised.exception.code, 130)
                process.send_signal.assert_called_once_with(signum)
                process.wait.assert_called_once_with()

    def test_batch_callback_restores_handlers_on_spawn_failure(self):
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        with mock.patch.object(idf_ext.subprocess, "Popen", side_effect=OSError):
            with self.assertRaises(OSError):
                self.actions["tuya-batch-flash"]["callback"](
                    "tuya-batch-flash", self.ctx, self.global_args)
        for sig, handler in previous.items():
            self.assertIs(signal.getsignal(sig), handler)

    def test_extension_load_does_not_import_batch_dependencies(self):
        imported = []
        real_import = builtins.__import__

        def reject_batch_imports(name, *args, **kwargs):
            if name in ("openpyxl", "serial"):
                imported.append(name)
                raise AssertionError("batch dependency imported eagerly")
            return real_import(name, *args, **kwargs)

        module = importlib.util.module_from_spec(SPEC)
        with mock.patch("builtins.__import__", side_effect=reject_batch_imports):
            SPEC.loader.exec_module(module)
            actions = module.action_extensions({}, str(PROJECT_DIR))["actions"]
        self.assertEqual(imported, [])
        self.assertIn("tuya-auth-flash", actions)
        self.assertIn("tuya-auth-read", actions)
        self.assertIn("tuya-batch-setup", actions)
        self.assertIn("tuya-batch-flash", actions)


@unittest.skipUnless(os.name == "posix", "requires POSIX signals")
class TestRealBatchCancellation(unittest.TestCase):
    def test_idf_interrupt_waits_for_backend_to_reap_detached_tool(self):
        self.check_cancellation(whole_group=False)

    def test_terminal_group_interrupt_waits_for_backend_cleanup(self):
        self.check_cancellation(whole_group=True)

    def test_idf_termination_waits_for_backend_to_reap_detached_tool(self):
        self.check_cancellation(whole_group=False, signum=signal.SIGTERM)

    def check_cancellation(self, whole_group, signum=signal.SIGINT):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            backend = pathlib.Path(tmp) / "synthetic_backend.py"
            backend.write_text(textwrap.dedent('''\
                import os, signal, subprocess, sys, threading, time
                sys.path.insert(0, %r)
                import tuya_auth_tool
                cancelled = threading.Event()
                signal.signal(signal.SIGINT, lambda *args: cancelled.set())
                signal.signal(signal.SIGTERM, lambda *args: cancelled.set())
                tool = subprocess.Popen(
                    [sys.executable, '-c', 'import time; time.sleep(30)'],
                    start_new_session=True)
                print(tool.pid, flush=True)
                cancelled.wait(20)
                # Longer than Popen's Ctrl-C grace period (0.25s).
                time.sleep(0.6)
                tuya_auth_tool._terminate_process(tool)
                print('reaped', flush=True)
                sys.exit(130)
                ''' % str(PROJECT_DIR / "scripts")), encoding="utf-8")
            wrapper = textwrap.dedent('''\
                import sys, types
                sys.path.insert(0, %r)
                import idf_ext
                idf_ext.BATCH_TOOL_PATH = sys.argv[1]
                args = types.SimpleNamespace(project_dir='.', build_dir='.',
                                             baud=115200, port=None)
                idf_ext._run_batch_tool(args, (), None, None, None, False,
                                       False, None, None, False, None)
                ''' % str(PROJECT_DIR))
            process = subprocess.Popen(
                [sys.executable, "-c", wrapper, str(backend)],
                start_new_session=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            tool_pid = None
            try:
                tool_pid = int(process.stdout.readline().strip())
                # Signal only IDF: it must forward cancellation even if the
                # backend did not receive a terminal foreground-group signal.
                if whole_group:
                    os.killpg(process.pid, signum)
                else:
                    process.send_signal(signum)
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 130, stderr)
                self.assertIn("reaped", stdout, stderr)
                with self.assertRaises(ProcessLookupError):
                    os.kill(tool_pid, 0)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if tool_pid is not None:
                    try:
                        os.kill(tool_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
