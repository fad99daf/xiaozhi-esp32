#include "tuya_auth.h"

#include "settings.h"

#include <esp_log.h>
#include <cstring>

#define TAG "TuyaAuth"

namespace {

struct FieldSpec {
    const char* nvs_key;
    char* dest;
    size_t cap;
    size_t min_len;
    const char* label;
};

// The BLE provisioning SDK copies fixed sizes (16/32/16 bytes) from these
// strings without regard for their C length, so a too-short value would read
// past the buffer into unrelated data. Enforce realistic minimum lengths.
bool LoadField(const FieldSpec& field) {
    std::memset(field.dest, 0, field.cap);

    std::string value;
    {
        Settings settings("tuya_auth", false);
        value = settings.GetString(field.nvs_key);
    }

    if (value.empty()) {
        ESP_LOGE(TAG, "%s missing (flash with: idf.py tuya-auth-flash)", field.label);
        return false;
    }
    if (value.size() < field.min_len || value.size() >= field.cap) {
        ESP_LOGE(TAG, "%s invalid length %d (allowed %d..%d)",
                 field.label, (int)value.size(), (int)field.min_len, (int)field.cap - 1);
        return false;
    }
    std::memcpy(field.dest, value.c_str(), value.size());
    return true;
}

} // namespace

bool TuyaAuthLoad(TuyaAuthCredentials& out) {
    FieldSpec fields[] = {
        { "uuid",        out.uuid,        sizeof(out.uuid),        16, "uuid" },
        { "auth_key",    out.auth_key,    sizeof(out.auth_key),    32, "auth_key" },
        { "product_key", out.product_key, sizeof(out.product_key), 16, "product_key" },
    };

    bool ok = true;
    for (auto& field : fields) {
        if (!LoadField(field)) {
            ok = false;
        }
    }
    if (!ok) {
        return false;
    }

    ESP_LOGI(TAG, "Auth loaded (uuid=%s product_key=%s)", out.uuid, out.product_key);
    return true;
}
