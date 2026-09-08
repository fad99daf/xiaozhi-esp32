#ifndef TUYA_AUTH_H
#define TUYA_AUTH_H

#include <stddef.h>

/*
 * Runtime Tuya device identity (uuid / auth_key / product_key).
 *
 * Read from the NVS partition (namespace "tuya_auth", keys
 * "uuid" / "auth_key" / "product_key"), flashed with:
 *     idf.py tuya-auth-flash
 *
 * The fixed-size zero-padded buffers matter: the BLE provisioning SDK
 * performs fixed-length reads (16/32/16 bytes) on these strings, so short
 * values are safe (tail reads zeros) instead of reading past a heap buffer.
 */
struct TuyaAuthCredentials {
    char uuid[32];         // matches iot_on_boarding_config_t.uuid
    char auth_key[64];     // matches iot_on_boarding_config_t.authkey
    char product_key[32];  // matches iot_on_boarding_config_t.product_key
};

/*
 * Loads the device identity from NVS into `out` (zero-padded char arrays).
 *
 * Returns true when all three fields are present and length-valid
 * (uuid: 16..31 chars, auth_key: 32..63 chars, product_key: 16..31 chars —
 * minimums come from the BLE SDK's fixed-length reads). On failure the
 * reason is logged and `out` must not be used.
 */
bool TuyaAuthLoad(TuyaAuthCredentials& out);

#endif // TUYA_AUTH_H
