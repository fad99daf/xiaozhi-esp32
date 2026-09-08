#include "tuya_ble_prov_wrapper.h"

#if CONFIG_TUYA_BLE_PROVISIONING

#include "tuya_auth.h"

extern "C" {
#include "tuya_ble_nimble.h"
#include "esp_bt.h"
}

#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <esp_log.h>
#include <cstring>

#define TAG "BleProv"
#define BLE_PROV_DONE_BIT BIT0

static EventGroupHandle_t s_event_group;
static BleProvResult s_result;

static void ble_prov_callback(const tuya_ble_wifi_creds_t *creds)
{
    s_result.ssid = creds->ssid;
    s_result.password = creds->password;
    s_result.token = creds->token;
    ESP_LOGI(TAG, "Received credentials: ssid=%s token=%s", creds->ssid, creds->token);
    xEventGroupSetBits(s_event_group, BLE_PROV_DONE_BIT);
}

static void ble_full_deinit(void)
{
    // nimble_port_deinit() (called by tuya_ble_nimble_stop) already does
    // esp_bt_controller_disable + esp_bt_controller_deinit internally.
    // Wait for async cleanup to complete.
    vTaskDelay(pdMS_TO_TICKS(500));

    // Release all BT memory (controller BSS/data + host sections).
    // This frees ~60KB of internal DRAM needed for WiFi/TLS/httpd.
    esp_err_t err = esp_bt_mem_release(ESP_BT_MODE_BLE);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "BT memory released");
    } else {
        ESP_LOGW(TAG, "esp_bt_mem_release failed: %s", esp_err_to_name(err));
        // Fallback: try controller-only release
        esp_bt_controller_mem_release(ESP_BT_MODE_BLE);
    }
}

bool TuyaBleProvision(int timeout_ms, BleProvResult& result)
{
    s_event_group = xEventGroupCreate();
    if (!s_event_group) return false;

    s_result = {};

    // tuya_ble_prov_cfg_t keeps const char* pointers for the whole
    // provisioning session, so the credentials must outlive TuyaBleProvision.
    static TuyaAuthCredentials auth;
    if (!TuyaAuthLoad(auth)) {
        ESP_LOGE(TAG, "Device identity unavailable - flash it with: idf.py tuya-auth-flash");
        vEventGroupDelete(s_event_group);
        s_event_group = nullptr;
        return false;
    }

    tuya_ble_prov_cfg_t cfg = {};
    cfg.device_name = "TUYA";
    cfg.product_key = auth.product_key;
    cfg.uuid = auth.uuid;
    cfg.auth_key = auth.auth_key;
    cfg.cb = ble_prov_callback;

    int rc = tuya_ble_nimble_start(&cfg);
    if (rc != 0) {
        ESP_LOGE(TAG, "tuya_ble_nimble_start failed: %d", rc);
        vEventGroupDelete(s_event_group);
        return false;
    }

    ESP_LOGI(TAG, "BLE advertising started, waiting %d ms...", timeout_ms);
    EventBits_t bits = xEventGroupWaitBits(
        s_event_group, BLE_PROV_DONE_BIT,
        pdTRUE, pdTRUE,
        pdMS_TO_TICKS(timeout_ms));

    tuya_ble_nimble_stop();
    ble_full_deinit();

    vEventGroupDelete(s_event_group);
    s_event_group = nullptr;

    if (bits & BLE_PROV_DONE_BIT) {
        result = s_result;
        ESP_LOGI(TAG, "BLE provisioning succeeded");
        return true;
    }

    ESP_LOGW(TAG, "BLE provisioning timed out");
    return false;
}

#endif // CONFIG_TUYA_BLE_PROVISIONING
