"""Regression tests for the project idf.py extension callbacks."""

import importlib.util
import os
import pathlib
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
        self.global_args = types.SimpleNamespace(port="/dev/global")
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


if __name__ == "__main__":
    unittest.main()
