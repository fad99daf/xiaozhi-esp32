"""Real IDF parser smoke tests; run with python3 in an activated IDF shell.

CI uses ESP-IDF 5.5.2. Backends, pip, and serial discovery are mocked; the
selected project is temporary and contains no workbook, credentials, or build.
"""

import builtins
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parent.parent
TUYA_ACTIONS = (
    "tuya-batch-flash", "tuya-batch-setup", "tuya-auth-flash", "tuya-auth-read",
)


class TestIdfBatchParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        idf_path = os.environ.get("IDF_PATH")
        if not idf_path or not (Path(idf_path) / "tools" / "idf.py").is_file():
            raise unittest.SkipTest("Activate ESP-IDF before running parser smoke tests")

        path_patch = mock.patch.object(sys, "path", [str(Path(idf_path) / "tools")] + sys.path)
        path_patch.start()
        cls.addClassCleanup(path_patch.stop)
        bytecode_patch = mock.patch.object(sys, "dont_write_bytecode", True)
        bytecode_patch.start()
        cls.addClassCleanup(bytecode_patch.stop)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        cls.addClassCleanup(env_patch.stop)
        try:
            from click.testing import CliRunner
            spec = importlib.util.spec_from_file_location(
                "batch_smoke_idf", Path(idf_path) / "tools" / "idf.py")
            cls.idf = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.idf)
        except (ImportError, SystemExit) as exc:
            raise unittest.SkipTest(
                "IDF parser unavailable; use the active IDF Python: %s" % exc)
        cls.runner = CliRunner()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="idf parser ", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name).resolve()
        shutil.copyfile(PROJECT_DIR / "idf_ext.py", self.project / "idf_ext.py")

        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for name in ("ESPPORT", "ESPBAUD", "IDF_EXTRA_ACTIONS_PATH", "_IDF.PY_COMPLETE"):
            os.environ.pop(name, None)
        # Unlike the idf.py script, unittest puts the repository on sys.path.
        # Remove it so IDF's -C extension lookup selects the isolated project.
        path_patch = mock.patch.object(sys, "path", [
            path for path in sys.path if Path(path).resolve() != PROJECT_DIR
        ])
        path_patch.start()
        self.addCleanup(path_patch.stop)
        modules_patch = mock.patch.dict(sys.modules)
        modules_patch.start()
        self.addCleanup(modules_patch.stop)
        sys.modules.pop("idf_ext", None)

        self.popen = self.start_patch(
            "subprocess.Popen", side_effect=AssertionError("Unexpected process launch"))
        self.run = self.start_patch(
            "subprocess.run", side_effect=AssertionError("Unexpected subprocess.run"))
        self.discovery = self.start_patch(
            "idf_py_actions.tools.get_default_serial_port",
            return_value="/dev/synthetic-discovered")
        self.dependency_imports = []
        self.allow_setup_imports = False
        real_import = builtins.__import__

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            root = name.split(".")[0]
            caller = (globals or {}).get("__name__", "")
            # IDF itself uses pyserial. Only the project must stay lazy about it.
            if root == "openpyxl" or (
                    root == "serial" and (caller == "idf_ext" or globals is None)):
                self.dependency_imports.append(root)
                if not self.allow_setup_imports:
                    raise AssertionError("Eager batch dependency import: " + name)
                return types.ModuleType(root)
            if root in ("batch_flash", "tuya_auth_tool"):
                raise AssertionError("Real backend must not be imported")
            return real_import(name, globals, locals, fromlist, level)

        self.start_patch("builtins.__import__", side_effect=guarded_import)

    def start_patch(self, target, **kwargs):
        patch = mock.patch(target, **kwargs)
        result = patch.start()
        self.addCleanup(patch.stop)
        return result

    def invoke(self, arguments, action=None):
        argv = ["-C", str(self.project)] + arguments
        # init_cli's preliminary -C parser reads sys.argv, not CliRunner's args.
        with mock.patch.object(sys, "argv", ["idf.py"] + argv):
            cli = self.idf.init_cli()
            self.assertEqual(Path(sys.modules["idf_ext"].__file__),
                             self.project / "idf_ext.py")
            forbidden = []
            for name, command in cli._actions.items():
                if name not in TUYA_ACTIONS and name != "help":
                    guard = mock.Mock(side_effect=AssertionError(
                        "Unexpected IDF action: " + name))
                    command.unwrapped_callback = guard
                    forbidden.append(guard)
            result = self.runner.invoke(cli, argv, standalone_mode=False)

        self.assertEqual(result.exit_code, 0, "%s\n%r" % (result.output, result.exception))
        for guard in forbidden:
            guard.assert_not_called()
        if action is not None:
            # Check the real dependency scheduler, not just the registration dict.
            self.assertEqual(list(result.return_value), [action])
        self.assertEqual(sorted(path.name for path in self.project.iterdir()), ["idf_ext.py"])
        return result

    def batch_command(self, arguments, global_options=()):
        self.popen.side_effect = None
        self.popen.return_value.wait.return_value = 0
        self.invoke(list(global_options) + ["tuya-batch-flash"] + arguments,
                    action="tuya-batch-flash")
        self.popen.assert_called_once()
        self.popen.return_value.wait.assert_called_once_with()
        self.run.assert_not_called()
        self.discovery.assert_not_called()
        self.assertEqual(self.dependency_imports, [])
        return self.popen.call_args.args[0]

    def batch_prefix(self, build_dir=None, baud="460800"):
        return [
            sys.executable, str(self.project / "scripts" / "batch_flash.py"),
            "--project-dir", str(self.project),
            "--build-dir", str(build_dir or self.project / "build"),
            "--baud", baud,
        ]

    def test_pid_only_dispatches_without_single_device_discovery_or_build(self):
        command = self.batch_command(["--pid", "synthetic-pid"])
        self.assertEqual(command, self.batch_prefix() + ["--pid", "synthetic-pid"])

    def test_repeated_devices_preserve_supplied_order(self):
        options = ["--pid", "synthetic-pid", "--device", "/dev/synthetic-z",
                   "--device", "/dev/synthetic-a"]
        self.assertEqual(self.batch_command(options), self.batch_prefix() + options)

    def test_project_build_and_baud_globals_reach_backend(self):
        build_dir = self.project / "output with spaces"
        command = self.batch_command(
            ["--pid", "synthetic-pid"], ["-B", str(build_dir), "-b", "921600"])
        self.assertEqual(command, self.batch_prefix(build_dir, "921600") +
                         ["--pid", "synthetic-pid"])

    def test_listing_does_not_schedule_build_or_require_pid(self):
        self.assertEqual(self.batch_command(["--list-ports"]),
                         self.batch_prefix() + ["--list-ports"])

    def test_action_dry_run_dispatches_backend_instead_of_idf_dry_run(self):
        options = ["--pid", "synthetic-pid", "--dry-run"]
        self.assertEqual(self.batch_command(options), self.batch_prefix() + options)

    def test_setup_invokes_pip_with_active_python_without_build(self):
        self.allow_setup_imports = True
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([], 0)
        self.invoke(["tuya-batch-setup"], action="tuya-batch-setup")
        self.run.assert_called_once_with([
            sys.executable, "-m", "pip", "install", "-r",
            str(self.project / "scripts" / "requirements-batch-flash.txt"),
        ])
        self.assertEqual(self.dependency_imports, ["openpyxl", "serial"])
        self.popen.assert_not_called()
        self.discovery.assert_not_called()

    def test_global_and_action_help_register_without_batch_dependencies(self):
        result = self.invoke(["--help"])
        for action in TUYA_ACTIONS:
            self.assertIn(action, result.output)
        for action, option in (
                ("tuya-batch-flash", "--device"),
                ("tuya-batch-setup", "--help"),
                ("tuya-auth-flash", "--file"),
                ("tuya-auth-read", "--port")):
            with self.subTest(action=action):
                result = self.invoke([action, "--help"])
                self.assertIn(option, result.output)
        self.assertEqual(self.dependency_imports, [])
        self.popen.assert_not_called()
        self.run.assert_not_called()
        self.discovery.assert_not_called()

    def test_old_auth_actions_dispatch_without_batch_dependencies(self):
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([], 0)
        self.invoke(["-p", "/dev/synthetic-explicit", "tuya-auth-flash",
                     "--file", "synthetic-unused.txt", "--no-verify"],
                    action="tuya-auth-flash")
        self.run.assert_called_once_with([
            sys.executable, str(self.project / "scripts" / "tuya_auth_tool.py"),
            "write", "--port", "/dev/synthetic-explicit",
            "--file", "synthetic-unused.txt", "--no-verify",
        ])
        self.discovery.assert_not_called()
        self.run.reset_mock()
        self.invoke(["tuya-auth-read"], action="tuya-auth-read")
        self.discovery.assert_called_once_with()
        self.run.assert_called_once_with([
            sys.executable, str(self.project / "scripts" / "tuya_auth_tool.py"),
            "read", "--port", "/dev/synthetic-discovered",
        ])
        self.assertEqual(self.dependency_imports, [])
        self.popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
