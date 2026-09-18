# Batch Firmware and Tuya Credential Flashing

The normal batch workflow is:

```sh
# Once per ESP-IDF Python environment
idf tuya-batch-setup

# Build the intended board configuration once
idf build

# Discover, confirm, probe, flash, and verify the connected batch
idf tuya-batch-flash --pid YOUR_PRODUCT_PID
```

The command uses the current `-C/--project-dir`, `-B/--build-dir`, and `-b/--baud` IDF settings. It does not build firmware automatically. The selected build directory must already contain a complete `flasher_args.json` and every image listed by that manifest.

## Before Each Batch

- Close Excel/Numbers and any serial monitor using the devices.
- Connect only the intended, compatible boards. Unplug unrelated USB serial equipment and boards already programmed by a previous batch.
- Confirm that the current build is for the exact board configuration and PID being manufactured. Matching chip family alone does not prove that board wiring is compatible.
- Keep devices and cables fixed for the whole run. Newly connected ports are not enrolled, and a vanished port is never replaced by another candidate.
- Use a powered hub or reduce `--jobs` if the bench cannot reliably power or program all devices concurrently.

Flashing the identity replaces the complete `nvs` partition. WiFi settings, device settings, and Tuya activation state are erased; each device must pair with the Tuya app again.

## Workbook

The default workbook is `auth-info.xlsx` in the project root. It must contain one worksheet with text columns named `uuid` and `key`; use `--sheet NAME` if more than one worksheet matches. The spreadsheet `key` maps to the device `auth_key`. Use `--xlsx PATH` to select another workbook.

Credential cells must be plain text with no formulas, surrounding whitespace, or partial rows. The tool rejects duplicate UUIDs/keys and invalid lengths without printing secret values. Validation and chip-binding checks cover every credential-bearing worksheet, including hidden sheets; `--sheet` only selects the allocation/retry pool and cannot bypass an existing binding. It preserves row order, other columns, other worksheets, and ordinary formatting.

The tool appends these tracking columns when needed:

| Column | Meaning |
|---|---|
| `status` | `unused`, `in_progress`, `used`, or `fail` |
| `stage` | `reserved`, `firmware`, `auth_write`, `auth_read`, `auth_verify`, or `complete` |
| `error` | Short sanitized failure reason |
| `port` | Full serial path used for the attempt |
| `usb_serial`, `usb_location` | USB metadata when available |
| `device_mac` | Probed chip identity and authoritative retry binding |
| `product_key` | PID assigned to the row |
| `run_id`, `updated_at` | Run identifier and UTC update time |

A row becomes `used` only after complete firmware flashing, NVS writing, byte-for-byte image read-back and exact identity comparison, final reset, and successful workbook persistence. Image comparison also detects corrupt NVS metadata/CRCs. The NVS tooling uses the partition-table offset from the manifest rather than assuming `0x8000`. Firmware and intermediate NVS operations suppress application startup; only after verification does the tool apply the manifest's final reset setting. A reset/reconnect failure is reported at `auth_verify`, not as a pass. A failed or interrupted row is never automatically recycled.

The tool holds an advisory lock beside the workbook from allocation through completion. Before probing it creates an owner-only pre-run backup. Reservations and outcomes are first appended to an owner-only, secret-free journal, then saved to the workbook through a same-directory temporary file and atomic replacement. Do not restore the backup automatically after devices have been programmed: doing so could make consumed identities appear unused.

Default companion files are:

```text
auth-info.xlsx.lock
auth-info.xlsx.batch-backup.xlsx
auth-info.xlsx.batch-journal.jsonl
```

Editors that ignore advisory locks can still conflict. If the workbook changes on disk, the command refuses to overwrite it. A persistence failure cancels remaining work and prints `RESULT NOT SAVED`; inspect the workbook and journal before continuing.

## Discovery and Safety Barrier

With no `--device` options, the tool discovers USB-backed serial candidates. On macOS it prefers `/dev/cu.*` callout paths and removes matching `/dev/tty.*` aliases. On Linux it selects USB serial/ACM interfaces rather than built-in UART or Bluetooth endpoints. Candidates are sorted by full path for deterministic row allocation.

USB metadata only identifies a host interface; it does not prove an ESP is attached or identify the board model. Before opening any port, the command displays the full path, description, VID/PID, USB serial/location when available, and provisional workbook row/UUID. It never displays auth keys.

After confirmation, every selected candidate is probed. Probing may reset a connected device but does not write flash. This is an all-device barrier:

- A busy, unresponsive, wrong-chip, duplicate-chip, or already-bound device rejects the whole preflight.
- No firmware is written and no credential row is reserved if any probe fails.
- After the barrier passes, assignments are bound durably to chip MAC addresses before concurrent workers start.
- Once programming starts, one device may fail without stopping healthy peers. Its row becomes `fail` if persistence remains available.

The prompt warns that all listed candidates will be probed and, if compatible, programmed. Noninteractive flashing requires `--yes`. For unattended runs, also provide explicit `--device` options so unrelated devices cannot be selected accidentally.

## Inspection and Selection

List candidates without requiring a PID, workbook, build, spreadsheet dependency, or device probe:

```sh
idf tuya-batch-flash --list-ports
```

Validate local dependencies, PID, workbook, manifest, and provisional allocation without opening/resetting ports or modifying the workbook:

```sh
idf tuya-batch-flash --pid YOUR_PRODUCT_PID --dry-run
```

Limit a run to explicit full paths. Argument order controls row allocation:

```sh
idf tuya-batch-flash --pid YOUR_PRODUCT_PID \
  --device /dev/cu.usbserial-001 \
  --device /dev/cu.usbserial-002
```

Do not use IDF's global `-p/--port` or `ESPPORT` for batch flashing. Omit it for discovery or use repeated `--device PORT` options. Clear `ESPPORT` if it supplies a global port.

Useful controls:

```sh
idf -B build-board -b 460800 tuya-batch-flash \
  --pid YOUR_PRODUCT_PID --jobs 2 --timeout 600
```

- `--jobs N` limits concurrent device pipelines; the default is the selected device count.
- `--timeout SECONDS` sets a finite positive per-command timeout (currently 600 seconds; probes are capped at 60 seconds). These are provisional defaults pending bench validation.
- `--yes` skips the interactive confirmation but not validation or probing.
- `--xlsx PATH` and `--sheet NAME` select the credential source.

## Output and Progress

Progress is stage-level: it reports discovery/probing and the programming stages, elapsed time, and per-device outcomes. It does not show esptool byte percentages or an ETA.

On an interactive terminal the tool draws an in-place table during probing and flashing: a header with phase and counts, followed by one row per device (workbook row, current operation, elapsed time). Live progress is only used when all selected devices fit the terminal; a small terminal, `TERM=dumb`, or a very large batch falls back to plain event lines, and a mid-run resize switches to plain lines rather than drawing over unknown geometry. `NO_COLOR` disables color independently of cursor handling.

When output is redirected (CI, `| cat`, log files) the tool prints one line per event instead, with the same information:

```text
[probe] /dev/cu.usbserial-001 (row 2) | response received
[auth_read] /dev/cu.usbserial-001 (row 2) | reading back credentials | run elapsed 00:42
[PASS] /dev/cu.usbserial-001 (row 2) | PASS (verified) | duration 00:51 | finished 1/2
Flashing complete: finished 2/2 | pass 2 | fail 0 | cancelled 0 | unsaved 0
```

Reading the counters:

- During probing, `responded N/M` counts collected probe responses, including errors and cancellations. A response is not approval to flash: cross-device validation still runs before anything is reserved.
- During flashing, `finished N/M` counts recorded outcomes and can include failures; `PASS` means the credentials were written, read back, verified, the device reset, and the workbook saved — not merely that an image was written.
- `elapsed` is total run time; the per-device number on a row is the current operation's duration (or the device's total for a finished row). `queued` rows have started waiting but not yet entered a stage.
- Cancellation shows a `Stopping; waiting for active commands to exit.` notice while in-flight commands settle; already saved passes remain `used`.
- The aggregate `Flashing complete` footer is only printed after the run's records are committed, so a late persistence failure surfaces as `RESULT NOT SAVED` instead of being preceded by a success summary.

Persisted `stage`/`status` values and the final `--dry-run`/report formats are unchanged by the display.

## Failures, Cancellation, and Retry

The final report uses full port paths and always prints `PASS`, `FAIL`, or `RESULT NOT SAVED`. Failed-device details include the stage plus USB location, USB serial, and chip MAC when known. Port paths can change after reconnect and may not map visibly to a physical board. After the batch has stopped, unplug/reconnect one board at a time and rerun `--list-ports` if the bench metadata is insufficient.

Exit codes are:

| Code | Meaning |
|---|---|
| `0` | Every selected device verified and workbook outcomes were saved |
| `1` | Device-stage or persistence failure |
| `2` | Invalid input or rejected preflight |
| `130` | Cancellation |

Ctrl-C or SIGTERM stops scheduling, terminates active tool process groups, and waits for subprocess cleanup through the IDF wrapper (exit 130). Repeated signals do not interrupt an atomic workbook update. Cancelled, unfinished rows remain `in_progress`; they are not reused automatically. Rows already verified and saved remain `used`.

Retry exactly one failed or interrupted row against the same chip identity and original PID:

```sh
idf tuya-batch-flash \
  --pid ORIGINAL_PRODUCT_PID \
  --retry-uuid UUID_FROM_FAILED_ROW \
  --device /dev/cu.usbserial-current-path
```

A renamed port is acceptable only when probing finds the recorded chip MAC. `used` rows, different chips, different PIDs, and retries without exactly one explicit device are rejected.

## Hardware Acceptance

Synthetic host tests cover discovery, manifest execution, workbook safety, probe barriers, concurrency, cancellation, and recovery. Before production use, validate the actual supported USB bridges/native USB boards on the manufacturing bench, including reset/re-enumeration behavior, reliable baud/timeout values, hub power, safe concurrency, complete firmware/assets boot, and whether exposed USB location metadata is useful for physical identification.
