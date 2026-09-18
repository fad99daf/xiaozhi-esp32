"""Additional synthetic regressions for batch interruption and durability."""

import errno
import threading
from unittest import mock

from test_batch_flash import SyntheticCase, batch, credential, tracked


class TestBatchSafety(SyntheticCase):
    def test_final_reset_follows_verification_and_precedes_saved_success(self):
        self.enable_devices()
        verified = set()

        def auth(path, baud, values, **kwargs):
            self.assertEqual(kwargs["before"], "default_reset")
            self.assertEqual(kwargs["after"], "no_reset")
            self.assertEqual(kwargs["partition_table_offset"], 0x8000)
            self.good_auth(path, baud, values, **kwargs)
            verified.add(path)

        def finalize(assignment, manifest, baud, timeout, event):
            self.assertIn(assignment.candidate.path, verified)
            self.assertEqual(self.load().rows[assignment.row.index].status, "in_progress")

        self.auth.side_effect = auth
        self.finalize.side_effect = finalize
        self.assertEqual(batch.main(self.argv()), 0)
        self.assertEqual(self.finalize.call_count, 2)

    def test_final_reset_command_honors_manifest_without_writing(self):
        assignments, coordinator = self.assignments_and_coordinator()
        self.commands.side_effect = None
        manifest = batch.Manifest(str(self.build)).load()
        for after in ("hard_reset", "no_reset", "soft_reset"):
            manifest.after = after
            with mock.patch.object(batch, "verify_candidates_frozen") as frozen:
                batch.finalize_device(assignments[0], manifest, 115200, 9,
                                      coordinator.cancel_event)
            frozen.assert_called_once_with([assignments[0].candidate])
            command, stage = self.commands.call_args.args
            self.assertEqual(stage, "auth_verify")
            self.assertEqual(command[-1], "read_mac")
            self.assertEqual(command[command.index("--after") + 1], after)
            self.assertEqual(command[command.index("--before") + 1], "default_reset")
            self.assertEqual(command[command.index("--baud") + 1], "115200")
            self.assertIn("--no-stub", command)

    def test_failed_verification_never_resets_and_final_reset_failure_is_not_pass(self):
        self.enable_devices()
        self.auth.side_effect = batch.AuthToolError("auth_verify", "mismatch")
        self.assertEqual(batch.main(self.argv()), 1)
        self.finalize.assert_not_called()
        self.make_workbook([credential(1), credential(2)])
        self.auth.side_effect = self.good_auth
        self.finalize.side_effect = batch.AuthToolError("auth_verify", "reset failed")
        self.assertEqual(batch.main(self.argv()), 1)
        saved = self.load()
        self.assertEqual([saved.rows[i].status for i in (2, 3)], ["fail", "fail"])
        self.assertEqual(saved.rows[2].stage, "auth_verify")

    def test_nondefault_partition_table_offset_reaches_auth_pipeline(self):
        self.enable_devices()
        self.manifest_data["flash_files"]["0x9000"] = self.manifest_data["flash_files"].pop("0x8000")
        self.manifest_data["partition-table"]["offset"] = "0x9000"
        self.save_manifest()
        self.assertEqual(batch.main(self.argv()), 0)
        for call in self.auth.call_args_list:
            self.assertEqual(call.kwargs["partition_table_offset"], 0x9000)

    def test_persistence_failure_remains_terminal_even_if_storage_recovers(self):
        assignments, coordinator = self.assignments_and_coordinator()
        with mock.patch.object(coordinator.workbook, "save", side_effect=OSError("disk full")):
            with self.assertRaises(batch.PersistenceError):
                coordinator.finish(assignments[0], "used", "complete", None)
        journal_before = self.journal_records()
        with mock.patch.object(coordinator.workbook, "save") as save:
            with self.assertRaises(batch.PersistenceError):
                coordinator.finish(assignments[1], "used", "complete", None)
            with self.assertRaises(batch.PersistenceError):
                coordinator.set_stage(assignments[1], "auth_write")
        save.assert_not_called()
        self.assertEqual(self.journal_records(), journal_before)
        self.assertEqual([self.load().rows[i].status for i in (2, 3)],
                         ["in_progress", "in_progress"])

    def test_directory_fsync_ignores_unsupported_but_not_storage_errors(self):
        for number in (errno.EINVAL, errno.ENOTSUP, errno.EIO, errno.ENOSPC):
            with self.subTest(errno=number), mock.patch.object(
                    batch.os, "fsync", side_effect=OSError(number, "synthetic failure")):
                if number in (errno.EINVAL, errno.ENOTSUP):
                    batch._fsync_dir(str(self.root))
                else:
                    with self.assertRaises(OSError):
                        batch._fsync_dir(str(self.root))

    def test_cancelled_confirmation_returns_without_backup_or_probe(self):
        self.enable_devices()
        args = self.argv()
        args.remove("--yes")
        original = self.xlsx.read_bytes()

        original_confirm = batch._confirm
        def interrupted_confirm(event):
            event.set()
            original_confirm(event)

        with mock.patch.object(batch, "_confirm", side_effect=interrupted_confirm), \
                mock.patch.object(batch, "write_backup") as backup:
            self.assertEqual(batch.main(args), 130)
        backup.assert_not_called()
        self.probes.assert_not_called()
        self.assertEqual(self.xlsx.read_bytes(), original)

    def test_waiting_prompt_observes_cancellation_without_input(self):
        event = threading.Event()
        def select(*_args):
            event.set()
            return [], [], []
        with mock.patch.object(batch.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(batch.select, "select", side_effect=select), \
                self.assertRaises(batch.CommandCancelled):
            batch._confirm(event)

    def test_tracking_metadata_is_text_not_an_excel_formula(self):
        self.candidates[0].serial_number = '=HYPERLINK("synthetic")'
        assignments, coordinator = self.assignments_and_coordinator()
        coordinator.finish(assignments[0], "fail", "auth_read", "=synthetic-error")
        loaded = self.load()
        for field, value in (("usb_serial", self.candidates[0].serial_number),
                             ("error", "=synthetic-error")):
            cell = loaded.ws.cell(2, loaded.header[field])
            self.assertEqual(cell.value, value)
            self.assertEqual(cell.data_type, "s")

    def test_preview_includes_both_metadata_fields_warnings_and_progress(self):
        self.enable_devices()
        self.assertEqual(batch.main(self.argv()), 0)
        output = self.output.getvalue()
        for candidate in self.candidates:
            self.assertIn(candidate.serial_number, output)
            self.assertIn(candidate.location, output)
            self.assertIn("[auth_read] " + candidate.path, output)
        self.assertIn("entire NVS", output)
        self.assertIn("board variant", output)
        self.make_workbook([tracked(1, "in_progress"), credential(2), credential(3)])
        self.assertEqual(batch.main(self.argv("--dry-run")), 0)
        self.assertIn("interrupted in_progress rows 2", self.errors.getvalue())

    def test_reject_nonfinite_timeout_formula_pid_and_global_listing_port(self):
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value), self.assertRaises(SystemExit) as raised:
                self.args("--timeout=" + value)
            self.assertEqual(raised.exception.code, 2)
        self.assertEqual(batch.main(self.argv("--pid", "=" + "a" * 16)), 2)
        self.assertEqual(batch.main(["--list-ports", "--port", "/dev/wrong"]), 2)
        self.commands.assert_not_called()

    def test_manifest_rejects_invalid_settings_before_device_access(self):
        original = dict(self.manifest_data["extra_esptool_args"])
        for field, value in (("chip", 3), ("chip", "auto"), ("before", []),
                             ("after", "bad"), ("stub", "false"), ("force", True)):
            with self.subTest(field=field, value=value):
                self.manifest_data["extra_esptool_args"] = dict(original, **{field: value})
                self.save_manifest()
                self.assertEqual(batch.main(self.argv()), 2)
        self.commands.assert_not_called()

    def test_manifest_rejects_empty_overlapping_and_missing_partition_images(self):
        (self.build / "bootloader.bin").write_bytes(b"")
        with self.assertRaisesRegex(batch.BatchError, "empty"):
            batch.Manifest(str(self.build)).load()
        (self.build / "bootloader.bin").write_bytes(b"a" * 0x8001)
        with self.assertRaisesRegex(batch.BatchError, "overlapping"):
            batch.Manifest(str(self.build)).load()
        (self.build / "bootloader.bin").write_bytes(b"a")
        del self.manifest_data["partition-table"]
        self.save_manifest()
        with self.assertRaisesRegex(batch.BatchError, "partition-table"):
            batch.Manifest(str(self.build)).load()
