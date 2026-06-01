#ifndef TUYA_BLE_PROV_WRAPPER_H
#define TUYA_BLE_PROV_WRAPPER_H

#include <string>

struct BleProvResult {
    std::string ssid;
    std::string password;
    std::string token;
};

// Blocking call. Starts BLE advertising, waits up to timeout_ms for
// the Tuya app to send WiFi credentials + activation token.
// Stops BLE before returning regardless of outcome.
// Returns true on success (result populated), false on timeout.
bool TuyaBleProvision(int timeout_ms, BleProvResult& result);

#endif // TUYA_BLE_PROV_WRAPPER_H
