"""Synthetic cross-sheet binding and esptool identity regressions."""

import threading
import types
import unittest

from test_batch_flash import SyntheticCase, batch, credential, openpyxl, tracked


class TestWorkbookBindings(SyntheticCase):
    def add_sheet(self, name, rows, header_row=1, hidden=False, headers=None):
        workbook = openpyxl.load_workbook(self.xlsx)
        sheet = workbook.create_sheet(name)
        headers = headers or ["uuid", "key"] + list(batch.TRACKING_FIELDS)
        for _ in range(header_row - 1):
            sheet.append(["Synthetic notes"])
        sheet.append(headers)
        for row in rows:
            sheet.append([row.get(field) for field in headers])
        if hidden:
            sheet.sheet_state = "hidden"
        workbook.save(self.xlsx)
        workbook.close()

    def load_selected(self, write=False):
        workbook = batch.Workbook(str(self.xlsx), "Credentials").load(for_write=write)
        self.addCleanup(workbook.wb.close)
        return workbook

    def test_duplicates_across_sheets_are_rejected_without_values(self):
        for field in ("uuid", "key", "device_mac"):
            with self.subTest(field=field):
                first, second = tracked(1, "used"), tracked(2, "fail")
                second[field] = first[field]
                if field == "device_mac":
                    second[field] = "0200.0000.0001"
                self.make_workbook([first])
                self.add_sheet("Archive", [second], header_row=3, hidden=True)
                with self.assertRaisesRegex(batch.BatchError, "duplicate " + field) as caught:
                    self.load_selected()
                detail = str(caught.exception)
                for location in ("Credentials", "Archive", "row 2", "row 4"):
                    self.assertIn(location, detail)
                for row in (first, second):
                    self.assertNotIn(row["uuid"], detail)
                    self.assertNotIn(row["key"], detail)
                self.commands.assert_not_called()

    def test_duplicates_between_two_unselected_sheets_are_rejected(self):
        self.add_sheet("Archive", [credential(20)])
        self.add_sheet("Other", [credential(20)], hidden=True)
        with self.assertRaisesRegex(batch.BatchError, "duplicate uuid") as caught:
            self.load_selected()
        self.assertIn("Archive", str(caught.exception))
        self.assertIn("Other", str(caught.exception))

    def test_allocation_and_tracking_columns_remain_selected_sheet_only(self):
        self.make_workbook([credential(1)], headers=["uuid", "key"])
        self.add_sheet("Other", [credential(2)], header_row=3, headers=["uuid", "key"])
        workbook = self.load_selected(write=True)
        self.assertEqual(list(workbook.rows), [2])
        self.assertEqual(set(workbook.all_rows), {("Credentials", 2), ("Other", 4)})
        self.assertIs(workbook.rows[2], workbook.all_rows[("Credentials", 2)])
        assignment = batch.plan_assignments(self.args(), workbook, self.candidates[:1])[0]
        self.assertEqual(assignment.row.uuid, credential(1)["uuid"])
        with self.assertRaisesRegex(batch.BatchError, "not enough unused"):
            batch.plan_assignments(self.args(), workbook, self.candidates)
        workbook.save()
        saved = self.load_selected()
        self.assertEqual(saved.rows[2].sheet_title, "Credentials")
        self.assertEqual(saved.wb["Other"].max_column, 2)
        self.assertTrue(all(field in saved.header for field in batch.TRACKING_FIELDS))

    def test_other_sheet_binding_blocks_flash_before_reservation(self):
        self.enable_devices()
        for status in ("used", "fail", "in_progress"):
            with self.subTest(status=status):
                self.make_workbook([credential(10), credential(11)])
                self.add_sheet("Archive", [tracked(1, status)], hidden=True)
                before = self.xlsx.read_bytes()
                self.assertEqual(batch.main(self.argv("--sheet", "Credentials")), 2)
                self.assertEqual(self.xlsx.read_bytes(), before)
                self.commands.assert_not_called()
                self.auth.assert_not_called()
                self.assertIn("already bound", self.errors.getvalue())
                self.assertIn("Archive", self.errors.getvalue())
                self.assertFalse(any(record["event"] == "reserve"
                                     for record in self.journal_records()))

    def test_successful_flash_updates_only_selected_sheet(self):
        self.make_workbook([credential(10), credential(11)])
        self.add_sheet("Archive", [tracked(20, "used"), credential(21)])
        original = self.load_selected()
        archive_values = list(original.wb["Archive"].values)
        self.enable_devices()
        self.assertEqual(batch.main(self.argv("--sheet", "Credentials")), 0)
        saved = self.load_selected()
        self.assertEqual([row.status for row in saved.rows.values()], ["used", "used"])
        self.assertEqual(list(saved.wb["Archive"].values), archive_values)
        self.assertTrue(all(record["sheet"] == "Credentials"
                            for record in self.journal_records()))

    def test_retry_compares_sheet_and_row_not_just_row_number(self):
        self.make_workbook([tracked(1, "fail")])
        self.add_sheet("Archive", [tracked(2, "used")])
        workbook = self.load_selected()
        args = self.args("--sheet", "Credentials", "--retry-uuid", credential(1)["uuid"],
                         "--device", self.candidates[0].path)
        manifest = batch.Manifest(str(self.build)).load()
        results = {self.candidates[0].path: batch.ProbeResult("ESP32-S3", tracked(2, "used")["device_mac"])}
        with self.assertRaisesRegex(batch.BatchError, "already bound") as caught:
            batch.validate_probes(results, manifest, workbook, args)
        self.assertIn("Archive", str(caught.exception))
        results[self.candidates[0].path].mac = tracked(1, "fail")["device_mac"]
        batch.validate_probes(results, manifest, workbook, args)
        args.retry_uuid = credential(2)["uuid"]
        with self.assertRaisesRegex(batch.BatchError, "no credential row"):
            batch.plan_assignments(args, workbook, self.candidates[:1])

    def test_unselected_schema_errors_identify_sheet_without_secrets(self):
        row = tracked(20, "used")
        row["device_mac"] = "invalid"
        self.add_sheet("Archive", [row])
        with self.assertRaisesRegex(batch.BatchError, "invalid device_mac") as caught:
            self.load_selected()
        self.assertIn("Archive", str(caught.exception))
        self.assertNotIn(row["key"], str(caught.exception))


class TestEsptoolBindings(SyntheticCase):
    def probe_output(self, output):
        self.commands.side_effect = None
        self.commands.return_value = types.SimpleNamespace(stdout=output, stderr="")
        return batch.probe_device(self.candidates[0], batch.Manifest(str(self.build)).load(),
                                  None, 9, threading.Event())

    def test_c6_extended_mac_uses_six_byte_base_mac(self):
        result = self.probe_output(
            "Chip type: ESP32-C6FH4 (QFN32) (revision v0.1)\n"
            "MAC: 02:AB:CD:FF:FE:EF:00:01\nBASE MAC: 02:AB:CD:EF:00:01\n")
        self.assertIsNone(result.error)
        self.assertEqual(result.mac, "02:ab:cd:ef:00:01")
        self.assertTrue(batch.chips_compatible("esp32c6", result.chip))

    def test_base_mac_preferred_over_ordinary_mac_in_either_order(self):
        lines = ["MAC: 02:00:00:00:00:02\n", "BASE MAC: 02:00:00:00:00:01\n"]
        for order in (lines, list(reversed(lines))):
            with self.subTest(order=order):
                result = self.probe_output("Chip is ESP32-C6FH8\n" + "".join(order))
                self.assertEqual(result.mac, "02:00:00:00:00:01")

    def test_ordinary_six_byte_fallback_but_not_extended_or_malformed_mac(self):
        result = self.probe_output("Chip is ESP8685 (QFN28)\nMAC: 02:AB:CD:EF:00:01\n")
        self.assertEqual(result.mac, "02:ab:cd:ef:00:01")
        self.assertTrue(batch.chips_compatible("esp32c3", result.chip))
        for value in ("02:AB:CD:FF:FE:EF:00:01", ":" * 17):
            with self.subTest(value=value):
                self.assertIsNotNone(self.probe_output("MAC: " + value + "\n").error)

    def test_explicit_package_aliases_do_not_accept_other_families_or_suffixes(self):
        aliases = {"esp32c3": ("ESP8685", "ESP32-C3"),
                   "esp32c6": ("ESP32-C6FH4", "ESP32-C6FH8", "ESP32-C6"),
                   "esp32": ("ESP32-D0WDQ6", "ESP32-PICO-D4", "ESP32-D0WD-V3")}
        for target, packages in aliases.items():
            for package in packages:
                with self.subTest(target=target, package=package):
                    self.assertTrue(batch.chips_compatible(target, package))
                    self.assertFalse(batch.chips_compatible(target, package + "UNKNOWN"))
                    for other in set(aliases) - {target}:
                        self.assertFalse(batch.chips_compatible(other, package))
        for package in ("ESP8684", "ESP32-C6FH16", "ESP32-C3FAKE", "ESP32-S3", None):
            self.assertFalse(batch.chips_compatible("esp32c3", package))


if __name__ == "__main__":
    unittest.main()
