"""Run the actual BLE scan callback with an initialized but stopped WiFi driver."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_audio_abort import method

ROOT = Path(__file__).resolve().parents[1]

STUBS = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#define ESP_OK 0
#define ESP_ERR_WIFI_STATE 0x3006
#define WIFI_MODE_NULL 0
#define WIFI_MODE_STA 1
#define WIFI_COUNTRY_POLICY_AUTO 0
#define WIFI_SCAN_TYPE_ACTIVE 0
#define ESP_LOGE(tag, ...) ((void)0)
#define ESP_LOGW(tag, ...) ((void)0)
#define ESP_LOGI(tag, ...) ((void)0)
typedef int esp_err_t;
typedef int wifi_mode_t;
typedef struct { char cc[3]; int schan, nchan, max_tx_power, policy; } wifi_country_t;
typedef struct { int scan_type; } wifi_scan_config_t;
static bool s_wifi_started, s_wifi_scan_pending;
static uint32_t s_wifi_scan_token;
static bool driver_started;
static int mode, starts, scans, mode_error, start_error, scan_error;
static int esp_wifi_get_mode(wifi_mode_t *out) { *out = mode; return ESP_OK; }
static int esp_wifi_set_mode(wifi_mode_t value) {
    if (mode_error) return mode_error;
    mode = value;
    return ESP_OK;
}
static int esp_wifi_start(void) {
    ++starts;
    if (start_error) return start_error;
    driver_started = true;
    return ESP_OK;
}
static int esp_wifi_set_country(const wifi_country_t *country) { return ESP_OK; }
static int esp_wifi_scan_start(const wifi_scan_config_t *config, bool block) {
    ++scans;
    assert(!block);
    if (!driver_started || mode != WIFI_MODE_STA) return -1;
    return scan_error;
}
'''

CASES = {
    'first_scan_starts_station': '''
        assert(wifi_scan_request(20, "US", 42, NULL) == 0);
        assert(driver_started && mode == WIFI_MODE_STA);
        assert(s_wifi_scan_pending && s_wifi_scan_token == 42);
    ''',
    'repeat_scan_reuses_station': '''
        assert(wifi_scan_request(20, "", 1, NULL) == 0);
        s_wifi_scan_pending = false;
        assert(wifi_scan_request(20, "", 2, NULL) == 0);
        assert(starts == 1 && scans == 2 && s_wifi_scan_token == 2);
    ''',
    'pending_scan_is_not_replaced': '''
        s_wifi_scan_pending = true;
        s_wifi_scan_token = 7;
        assert(wifi_scan_request(20, "", 8, NULL) != 0);
        assert(starts == 0 && scans == 0 && s_wifi_scan_token == 7);
    ''',
    'mode_failure_can_retry': '''
        mode_error = -1;
        assert(wifi_scan_request(20, "", 1, NULL) != 0);
        assert(!s_wifi_scan_pending && !s_wifi_started && scans == 0);
        mode_error = 0;
        assert(wifi_scan_request(20, "", 2, NULL) == 0);
    ''',
    'start_failure_can_retry': '''
        start_error = -1;
        assert(wifi_scan_request(20, "", 1, NULL) != 0);
        assert(!s_wifi_scan_pending && !s_wifi_started && scans == 0);
        start_error = 0;
        assert(wifi_scan_request(20, "", 2, NULL) == 0);
    ''',
    'scan_failure_clears_pending': '''
        scan_error = -1;
        assert(wifi_scan_request(20, "", 1, NULL) != 0);
        assert(!s_wifi_scan_pending);
        scan_error = 0;
        assert(wifi_scan_request(20, "", 2, NULL) == 0);
        assert(starts == 1 && s_wifi_scan_token == 2);
    ''',
}


class BleWifiScanTests(unittest.TestCase):
    def test_sdk_pairing_logs_visible_at_info_without_secrets(self):
        source = (ROOT / 'main/provisioning/tuya_ble_prov_wrapper.cc').read_text()
        callback = method(source, 'static void ble_sdk_log_cb(')
        harness = r'''
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
typedef enum { LOG_ERROR, LOG_WARN, LOG_INFO, LOG_DEBUG } log_level_t;
static char output[512];
static log_level_t output_level;
static int writes;
static void capture(log_level_t level, const char *fmt, ...) {
    if (level == LOG_DEBUG) return;  // Firmware's default INFO ceiling.
    output_level = level;
    ++writes;
    va_list args;
    va_start(args, fmt);
    vsnprintf(output, sizeof(output), fmt, args);
    va_end(args);
}
#define ESP_LOGE(tag, ...) capture(LOG_ERROR, __VA_ARGS__)
#define ESP_LOGW(tag, ...) capture(LOG_WARN, __VA_ARGS__)
#define ESP_LOGI(tag, ...) capture(LOG_INFO, __VA_ARGS__)
#define ESP_LOGD(tag, ...) capture(LOG_DEBUG, __VA_ARGS__)
'''
        cases = r'''
static void emit(log_level_t level, const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    ble_sdk_log_cb(level, fmt, args);
    va_end(args);
}
int main(void) {
    emit(LOG_DEBUG, "[ble] [PROTO] handshake_ready=%d", 1);
    assert(writes == 1 && output_level == LOG_INFO);
    assert(strcmp(output, "[ble] [PROTO] handshake_ready=1") == 0);
    emit(LOG_DEBUG, "[mqtt] unrelated debug");
    assert(writes == 1);
    emit(LOG_DEBUG, "[ble] HEX(%u): %s", 4u, "secret");
    assert(writes == 1);
    emit(LOG_DEBUG, "[ble] [PROTO] WiFi JSON: %s", "secret");
    assert(writes == 2 && output_level == LOG_INFO);
    assert(strstr(output, "redacted") && !strstr(output, "secret"));
    emit(LOG_ERROR, "[ble] JSON parse failed: %s", "secret");
    assert(writes == 3 && output_level == LOG_ERROR);
    assert(strstr(output, "parse failed") && !strstr(output, "secret"));
    emit(LOG_WARN, "[ble] [RX] CRC mismatch: %d", 42);
    assert(writes == 4 && output_level == LOG_WARN);
    assert(strstr(output, "42"));
    emit(LOG_INFO, "SDK initialized");
    assert(writes == 5 && strcmp(output, "SDK initialized") == 0);
    return 0;
}
'''
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            path = Path(directory)
            (path / 'test.cc').write_text(harness + callback + cases)
            subprocess.run(['c++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                            str(path / 'test.cc'), '-o', str(path / 'test')],
                           check=True, timeout=60, capture_output=True)
            subprocess.run([str(path / 'test')], check=True, timeout=10)

    def test_stop_releases_scan_resources_before_nimble(self):
        source = (ROOT / 'main/provisioning/tuya_ble_nimble.c').read_text()
        stop = method(source, 'int tuya_ble_nimble_stop(')
        stubs = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#define ESP_LOGI(tag, ...) ((void)0)
#define ESP_ERROR_CHECK(expr) assert((expr) == 0)
#define WIFI_EVENT 1
#define WIFI_EVENT_SCAN_DONE 2
static int host;
static int *s_host_task = &host;
static int s_host_stopped;
#define portMAX_DELAY 0
static int xSemaphoreTake(int sem, int timeout) { return 1; }
static void host_task_delete(void) { s_host_task = NULL; }
static bool s_prov_done, s_wifi_started = true;
static int netif, s_transport_timer;
static int *s_wifi_netif = &netif;
static bool handler_registered = true, scan_pending = true;
static int wifi_event_handler;
static int stop_error;
static int nimble_port_stop(void) { return stop_error; }
static void ble_npl_callout_stop(int *timer) {}
static void ble_npl_callout_deinit(int *timer) {}
static int esp_event_handler_unregister(int base, int id, int handler) {
    handler_registered = false;
    return 0;
}
static void wifi_scan_cancel(void) { scan_pending = false; }
static int esp_wifi_stop(void) {
    assert(!handler_registered && !scan_pending);
    return 0;
}
static void esp_netif_destroy_default_wifi(int *value) {
    assert(!s_wifi_started && value == &netif);
}
static void nimble_port_deinit(void) {
    assert(!handler_registered && !scan_pending && !s_wifi_started && !s_wifi_netif);
}
static void prov_buffers_free(void) {}
int main(void);
'''
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            path = Path(directory)
            (path / 'test.c').write_text(stubs + stop + r'''
int main(void) {
    // Failed host stop must preserve callback targets for a retry.
    stop_error = 42;
    assert(tuya_ble_nimble_stop() == 42);
    assert(s_host_task && handler_registered && scan_pending);
    stop_error = 0;
    assert(tuya_ble_nimble_stop() == 0);
    assert(!s_host_task && !handler_registered && !scan_pending);
    // No host was created (or already stopped): don't touch its event queue.
    assert(tuya_ble_nimble_stop() == 0);
    return 0;
}
''')
            subprocess.run(['cc', '-std=c11', str(path / 'test.c'), '-o', str(path / 'test')],
                           check=True, timeout=60, capture_output=True)
            subprocess.run([str(path / 'test')], check=True, timeout=10)

    def test_scan_lifecycle(self):
        source = (ROOT / 'main/provisioning/tuya_ble_nimble.c').read_text()
        callback = method(source, 'static int wifi_scan_request(')
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            path = Path(directory)
            for name, body in CASES.items():
                with self.subTest(name=name):
                    (path / 'test.c').write_text(STUBS + callback + '\nint main(void) {' + body + '}')
                    subprocess.run(['cc', '-std=c11', str(path / 'test.c'), '-o', str(path / 'test')],
                                   check=True, timeout=60, capture_output=True)
                    result = subprocess.run([str(path / 'test')], timeout=10, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr.decode())


if __name__ == '__main__':
    unittest.main()
