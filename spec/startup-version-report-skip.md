# Startup Version-Report Optimization Plan

## Goal
Use agentic-kit’s `skip_version_report` feature so normal boots avoid the two synchronous initialization reports (SDK metadata and firmware version), reducing startup latency, while preserving reports on the two cloud-state transitions that require them: the first successful user binding/network pairing and the first boot after a successful firmware OTA. The default behavior must remain conservative when the device’s reporting state cannot be established.

## Current State Analysis

### What Already Exists

- The vendored agentic-kit API exposes `skip_version_report` on both `iot_client_config_t` and `iot_on_boarding_config_t` (`components/esp-agentic-kit/agentic-kit/modules/iot-client/include/iot_client.h:186-234`). The documented meaning is `false` = report SDK metadata and firmware version during initialization, and `true` = skip both reports.
- The SDK applies this flag around both requests in `iot_client_init()` (`components/esp-agentic-kit/agentic-kit/modules/iot-client/src/iot_client.c:248-286`), so the optimization is one configuration decision and does not require changing agentic-kit internals.
- The normal Tuya startup path constructs an `iot_client_config_t` from persisted `tuya` NVS credentials and currently leaves the zero-initialized flag false (`main/protocols/tuya_protocol.cc:152-182`). This causes both reports on every normal protocol initialization.
- The first-binding BLE path uses `iot_on_boarding_config_t` in `TuyaProtocol::OnBoardWithToken()` (`main/protocols/tuya_protocol.cc:191-232`). It currently zero-initializes the config, so the onboarding flow reports by default after activation and then persists `devid`, `secret_key`, `local_key`, `region`, and `env` in the `tuya` namespace (`:218-226`).
- `iot_client_init_on_boarding_with_token()` propagates `config->skip_version_report` into the post-onboarding client initialization (`components/esp-agentic-kit/agentic-kit/modules/iot-client/src/iot_client.c:487-581`), making the first-bind exception implementable at the application call site.
- Startup performs OTA checking before protocol initialization: `Application::ActivationTask()` calls `CheckNewVersion()` and then `InitializeProtocol()` (`main/application.cc:329-352`). The Tuya OTA check creates a separate client (`main/ota.cc:524-571`) and currently also leaves `skip_version_report` false, meaning the OTA-check client itself performs the two reports before checking for an upgrade.
- Successful firmware OTA writes the next OTA partition and selects it as the boot partition (`main/ota.cc:297-416`), then the application reboots (`main/application.cc:1095-1109`). `Ota::MarkCurrentVersionValid()` only handles ESP OTA rollback validation (`main/ota.cc:277-295`); it does not persist an application-level “report after OTA” marker.
- `Settings` provides NVS-backed boolean/integer/string reads and writes (`main/settings.h:7-26`, `main/settings.cc:8-89`). The existing `tuya` namespace is already the persistence location for activation state, but the current cloud-reset path erases that namespace (`main/protocols/tuya_protocol.cc:355-378`), so any new marker there would also be cleared on unbind.

### What’s Missing

- The normal existing-credentials configuration does not set `skip_version_report = true`.
- There is no durable distinction between a normal reboot and the first boot following a successful firmware OTA.
- There is no explicit first-bind state decision in `OnBoardWithToken()`; the code relies on the default false rather than documenting and testing that this is intentional.
- The OTA-check client’s reporting behavior is not separated from the long-lived startup client. The implementation must decide whether the optimization applies to this short-lived OTA-check client too; because it is initialized before the OTA decision and currently performs the same two reports, leaving it unchanged may retain startup latency even after optimizing `TuyaProtocol`.

## Architecture / Data Flow

```text
Boot
  |
  v
Application::ActivationTask
  |-- CheckNewVersion
  |     |-- CheckTuyaVersion -> temporary iot_client_init
  |     |-- determine/report OTA marker policy
  |     `-- successful OTA -> persist pending version-report marker -> reboot
  |
  `-- InitializeProtocol -> TuyaProtocol::Start
        `-- InitIotClient -> iot_client_init
              |-- skip_version_report=false only when:
              |     * first successful user binding, or
              |     * pending marker from successful OTA
              `-- otherwise true (skip SDK meta + firmware reports)
```

## Phases

### Phase 1 — Define and persist the reporting-state contract

**Priority:** High  
**Effort:** Medium

1. Choose a single durable state representation for “the next client initialization must report.” Prefer an explicit boolean in a dedicated application-owned NVS namespace or a clearly named key in the existing `tuya` namespace; document its default and lifecycle. A missing/corrupt key must resolve to `true` (report) rather than silently suppressing a required report.
2. Define the exact event semantics:
   - Factory/unprovisioned device: first successful `OnBoardWithToken()` initialization reports (`false`), then clears the pending state only after the activation client was successfully created and its report path was allowed to run.
   - Already-bound device on an ordinary reboot/reconnect: skip (`true`).
   - Successful firmware OTA: persist a pending-report state before reboot; first post-OTA startup reports (`false`), then clears it only after the startup client initialization succeeds with reporting enabled.
   - Failed OTA, interrupted OTA, rollback, or non-firmware assets update: do not consume the marker.
3. Decide whether the marker should be tied to the firmware version rather than a bare boolean. A version string (for example, the last firmware version for which the report was intentionally enabled) is safer against repeated boots after a crash and makes the state observable, but the implementation should avoid adding more state than needed.
4. Keep cloud-reset/unbind behavior consistent: when `HandleCloudReset()` erases activation state, erase the reporting marker too if it lives in `tuya`; if it lives elsewhere, explicitly reset it to the first-bind/reporting default.

**Files to create/modify:**

- `main/protocols/tuya_protocol.cc` — consume/update the reporting-state decision and first-bind transition.
- `main/protocols/tuya_protocol.h` — add only a small helper declaration if needed.
- `main/settings.h` / `main/settings.cc` — no changes expected unless the chosen state needs an existing Settings capability not already available.
- `main/ota.cc` and/or `main/application.cc` — persist the post-OTA marker at the successful OTA boundary.
- `main/protocols/tuya_protocol.cc` — clear/reset the marker in cloud-reset handling if required by its namespace.

**Key design decisions:** report is opt-in for exceptional transitions; missing state is report-enabled; marker writes must occur at durable success boundaries, not merely when an OTA check finds an update.

### Phase 2 — Apply the skip flag to normal and first-bind client initialization

**Priority:** High  
**Effort:** Small

1. In `TuyaProtocol::InitIotClient()` (`main/protocols/tuya_protocol.cc:152-182`), set `cfg.skip_version_report` from the durable state before calling `iot_client_init()`.
2. Ensure the first post-OTA/first-bind report-enabled decision is consumed only after `iot_client_init()` returns a valid client. Do not clear it before initialization, because a failed initialization must be retried with reporting enabled.
3. In `TuyaProtocol::OnBoardWithToken()` (`main/protocols/tuya_protocol.cc:202-226`), explicitly set `cfg.skip_version_report = false` for the first user binding. This documents the required exception and ensures future default changes do not accidentally suppress the first report.
4. After successful onboarding, persist credentials and transition the marker so subsequent boots skip reports. Keep the existing credential persistence and client lifetime behavior unchanged.
5. Add logs that identify `reporting version/metadata` versus `skipping version/metadata`, without logging credentials or tokens.

**Files to create/modify:**

- `main/protocols/tuya_protocol.cc`
- `main/protocols/tuya_protocol.h` only if helper methods are introduced.

**Key design decisions:** use agentic-kit’s public flag rather than manually suppressing individual requests; preserve both reports together because the SDK flag intentionally skips both.

### Phase 3 — Handle the post-OTA exception at the correct boundary

**Priority:** High  
**Effort:** Medium

1. In the successful firmware OTA path (`main/application.cc:1095-1109` / `main/ota.cc:297-416`), persist the pending version-report state only after the image has been fully written, validated, and selected as the boot partition.
2. Do not set the marker when `CheckTuyaVersion()` merely discovers an available upgrade, when download/write fails, or when the device remains on the old image.
3. On the next boot, have the normal `TuyaProtocol::InitIotClient()` path read the marker and pass `skip_version_report = false`.
4. Clear/mark the pending state only after the report-enabled client initialization succeeds. If the client fails before reporting, leave the marker set for the next retry.
5. Review the rollback-validation sequence: `MarkCurrentVersionValid()` currently runs at the beginning of `CheckNewVersion()` (`main/application.cc:412-415`). Ensure the reporting marker is not cleared by a boot that is subsequently rolled back or otherwise fails before normal protocol startup.

**Files to create/modify:**

- `main/application.cc`
- `main/ota.cc` and `main/ota.h` if the OTA object owns the marker operation.
- `main/protocols/tuya_protocol.cc`

**Key design decisions:** the OTA marker represents “new firmware is now running and should be announced,” not “an OTA was requested.”

### Phase 4 — Prevent the OTA-check client from negating the startup improvement

**Priority:** High  
**Effort:** Medium

1. Confirm from runtime traces whether the temporary client created by `Ota::CheckTuyaVersion()` is part of the measured startup delay and whether its reports are required independently of the long-lived startup client.
2. If the desired behavior is to skip the two reports on ordinary boots globally, set `cfg.skip_version_report` in `CheckTuyaVersion()` based on the same state policy; otherwise document why this temporary client intentionally remains report-enabled.
3. For the first-bind case, note that `CheckTuyaVersion()` is skipped when no persisted `devid` exists (`main/ota.cc:533-538`), so first-binding reporting is provided by `OnBoardWithToken()`. For post-OTA and ordinary bound boots, apply the marker consistently to the temporary client if it is retained.
4. Avoid consuming the one-shot post-OTA marker in the temporary OTA-check client if the long-lived protocol client still needs to report; either have one clearly defined client own the consumption or use a state transition that guarantees exactly one report-enabled initialization.
5. Preserve the existing OTA status-report flow (`main/ota.cc:595-617`) and do not treat `skip_version_report` as a replacement for OTA upgrade checks or status reports.

**Files to create/modify:**

- `main/ota.cc`
- `main/protocols/tuya_protocol.cc`
- `main/application.cc` only if ownership of the marker transition changes.

**Key design decisions:** exactly one initialization in the startup sequence should consume the exceptional report opportunity; OTA check and OTA status reporting remain separate concerns.

### Phase 5 — Add regression coverage and measure startup impact

**Priority:** Medium  
**Effort:** Medium

1. Add host-side or SDK-side tests using the existing agentic-kit IoT client test patterns (`components/esp-agentic-kit/agentic-kit/modules/iot-client/test/iot_client_message_test.c:447-464`) to verify that `skip_version_report = true` suppresses both requests and `false` allows both.
2. Add application-level tests or a testable helper for the state machine covering at least:
   - missing marker/default state reports;
   - first bind reports and transitions to skip;
   - ordinary reboot skips;
   - successful OTA sets pending state;
   - first post-OTA initialization reports and consumes state;
   - failed initialization preserves pending state;
   - unbind/reset returns the device to first-bind behavior.
3. Verify that no report is skipped when NVS is unavailable, the marker is malformed, credentials are missing, or client initialization fails.
4. Build at least one Tuya-enabled representative target with the vendored agentic-kit submodule initialized. Use serial logs or network traces to count the two report requests and compare boot-to-ready time before and after the change.
5. Verify first pairing, ordinary reboot, OTA success/reboot, OTA failure, and cloud unbind/re-pair on hardware where available.

**Files to create/modify:**

- `tests/` — add focused tests if the application state helper can be isolated; otherwise rely on agentic-kit tests plus documented device verification.
- Relevant test build files only if required by the existing test harness.

## Key Design Decisions

- **Use the SDK flag as intended.** `skip_version_report` suppresses both SDK metadata and firmware-version requests atomically, matching the requested optimization and avoiding forked agentic-kit behavior.
- **Persist the exception, do not infer it from boot count.** A reboot is not equivalent to an OTA, and first binding is an application event. NVS state is already the project’s persistence mechanism for Tuya activation data.
- **Fail safe.** A missing or unreadable state must report rather than skip, because the SDK documentation says skipping is valid only when the cloud already has the current version.
- **Consume one-shot state after successful initialization.** This prevents a transient network/client failure from permanently suppressing the required report.
- **Keep OTA check/status traffic separate.** `iot_ota_check_upgrade()` and `iot_ota_report_status()` remain necessary even when initialization reports are skipped.
- **Do not alter legacy MQTT/WebSocket behavior unless the implementation confirms those paths also use agentic-kit.** The requested feature is in the Tuya/agentic-kit path grounded above.

## Dependency Considerations

No new library or service is required. The project already contains the required agentic-kit API and the `Settings` NVS wrapper. The implementation depends on using the agentic-kit revision currently present in `components/esp-agentic-kit/agentic-kit`; the submodule must remain initialized and its public headers must match the checked-in implementation.

## Risk Assessment

| Risk | Mitigation |
|---|---|
| A bare boolean is cleared after a failed initialization, permanently suppressing reports | Clear/advance the marker only after a valid report-enabled client is created; default missing state to report. |
| The temporary OTA-check client consumes or repeats the one-shot report state incorrectly | Define one owner for marker consumption and test the complete `CheckNewVersion()` → `InitializeProtocol()` sequence. |
| OTA success marker is set before the new image is actually bootable | Persist only after `esp_ota_end()` and `esp_ota_set_boot_partition()` succeed; retain it through rollback/pending verification until startup succeeds. |
| First pairing no longer reports because the onboarding config inherits a future/default skip value | Set `skip_version_report = false` explicitly in `OnBoardWithToken()` and test the first-bind path. |
| Cloud unbind leaves stale “already reported” state and suppresses the next user’s first report | Reset the marker together with activation state in `HandleCloudReset()`. |
| Startup appears unchanged because `CheckTuyaVersion()` still performs both reports | Measure and, if confirmed, apply the same policy to the temporary OTA-check client without losing the one-shot exception. |

## Success Criteria / Verification

- On a normal reboot of an already-bound device, agentic-kit logs show `skip_version_report = true` behavior and neither `atop_device_meta_save` nor `iot_ota_report_version` is requested during the relevant initialization.
- On first successful BLE/network pairing, both initialization reports occur exactly once, credentials are persisted, and the next reboot uses the skip path.
- After a successful firmware OTA and reboot, both reports occur once for the new firmware; a second reboot skips them.
- An OTA download, validation, or boot failure does not consume the pending-report exception.
- If the report-enabled initialization fails, the next attempt still reports.
- Cloud unbind followed by re-pairing returns to the first-bind behavior.
- Existing OTA upgrade checks, upgrade status reports, AI token acquisition, TAI context setup, and audio startup continue to work.
- The representative Tuya target builds successfully, the focused tests pass, and measured time from network-ready to activation/protocol-ready improves by approximately the latency of the two removed requests on ordinary boots.

## Out of Scope

- Changing agentic-kit’s request implementation, retry policy, cloud APIs, or protocol semantics.
- Removing OTA availability checks or OTA upgrade status reporting.
- Optimizing unrelated display, audio, BLE teardown, DNS, TLS, or TAI startup work.
- Extending the behavior to legacy MQTT/WebSocket protocols without evidence that they use the same agentic-kit client.
- Changing assets-version reporting or treating an assets-only update as a firmware OTA exception.
