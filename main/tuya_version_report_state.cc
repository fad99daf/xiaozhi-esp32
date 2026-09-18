#include "tuya_version_report_state.h"

#include <nvs.h>

#include <cstring>

namespace {

constexpr char kNamespace[] = "tuya";
constexpr char kReportedVersionKey[] = "version_report";
constexpr char kPendingVersionKey[] = "pending_report";

bool IsValidVersion(const char* version) {
    return version != nullptr && version[0] != '\0';
}

esp_err_t ReadString(nvs_handle_t handle, const char* key, char* value, size_t size) {
    size_t length = size;
    return nvs_get_str(handle, key, value, &length);
}

bool SetState(const char* key, const char* version) {
    if (!IsValidVersion(version)) {
        return false;
    }

    nvs_handle_t handle;
    if (nvs_open(kNamespace, NVS_READWRITE, &handle) != ESP_OK) {
        return false;
    }
    esp_err_t err = nvs_set_str(handle, key, version);
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    return err == ESP_OK;
}

}  // namespace

namespace TuyaVersionReportState {

bool ShouldReport(const char* current_version) {
    if (!IsValidVersion(current_version)) {
        return true;
    }

    nvs_handle_t handle;
    if (nvs_open(kNamespace, NVS_READONLY, &handle) != ESP_OK) {
        return true;
    }

    char reported_version[32] = {};
    esp_err_t reported_err = ReadString(handle, kReportedVersionKey,
                                        reported_version, sizeof(reported_version));
    if (reported_err != ESP_OK && reported_err != ESP_ERR_NVS_NOT_FOUND) {
        nvs_close(handle);
        return true;
    }

    char pending_version[32] = {};
    esp_err_t pending_err = ReadString(handle, kPendingVersionKey,
                                       pending_version, sizeof(pending_version));
    nvs_close(handle);
    if (pending_err != ESP_OK && pending_err != ESP_ERR_NVS_NOT_FOUND) {
        return true;
    }

    return std::strcmp(reported_version, current_version) != 0 ||
           std::strcmp(pending_version, current_version) == 0;
}

bool MarkPending(const char* target_version) {
    return SetState(kPendingVersionKey, target_version);
}

bool MarkReported(const char* current_version) {
    if (!IsValidVersion(current_version)) {
        return false;
    }

    nvs_handle_t handle;
    if (nvs_open(kNamespace, NVS_READWRITE, &handle) != ESP_OK) {
        return false;
    }

    esp_err_t err = nvs_set_str(handle, kReportedVersionKey, current_version);
    if (err == ESP_OK) {
        esp_err_t erase_err = nvs_erase_key(handle, kPendingVersionKey);
        if (erase_err != ESP_OK && erase_err != ESP_ERR_NVS_NOT_FOUND) {
            err = erase_err;
        }
    }
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    return err == ESP_OK;
}

}  // namespace TuyaVersionReportState
