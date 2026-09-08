"""Host tests for main/tuya_auth.cc against a mocked Settings (NVS).

Builds the real firmware loader with a stub Settings implementation backed by
an in-memory NVS, covering: successful NVS loads, missing/invalid values, and
partition-open failure. There is no compile-time fallback: credentials come
from NVS only.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_DIR = os.path.join(TEST_DIR, "..", "main")

STUB_SETTINGS_H = """\
#ifndef SETTINGS_STUB_H
#define SETTINGS_STUB_H
#include <string>
class Settings {
public:
    Settings(const std::string& ns, bool read_write = false);
    ~Settings();
    std::string GetString(const std::string& key, const std::string& default_value = "");
    void SetString(const std::string& key, const std::string& value);
    int32_t GetInt(const std::string& key, int32_t default_value = 0);
    void SetInt(const std::string& key, int32_t value);
    bool GetBool(const std::string& key, bool default_value = false);
    void SetBool(const std::string& key, bool value);
    void EraseKey(const std::string& key);
    void EraseAll();
private:
    std::string ns_;
};
#endif
"""

STUB_NVS_FLASH_H = """\
#ifndef NVS_FLASH_STUB_H
#define NVS_FLASH_STUB_H
#include <stdint.h>
typedef uint32_t nvs_handle_t;
#endif
"""

STUB_ESP_LOG_H = """\
#ifndef ESP_LOG_STUB_H
#define ESP_LOG_STUB_H
#include <cstdio>
#define ESP_LOGE(tag, fmt, ...) std::printf("E %s " fmt "\\n", tag, ##__VA_ARGS__)
#define ESP_LOGW(tag, fmt, ...) std::printf("W %s " fmt "\\n", tag, ##__VA_ARGS__)
#define ESP_LOGI(tag, fmt, ...) std::printf("I %s " fmt "\\n", tag, ##__VA_ARGS__)
#endif
"""

# Exposes nvs_store from the test binary so Python can drive it via env.
MOCK_SETTINGS_CC = r"""
#include "settings.h"
#include <string>
#include <map>
#include <vector>
#include <cstdlib>
#include <cstring>

std::map<std::string, std::map<std::string, std::string>> g_nvs_store;
std::map<std::string, bool> g_nvs_open_fail;

static void seed() {
    const char* env = std::getenv("NVS_SEED_JSON");
    if (env == nullptr) return;
    // Format: ns:key=value;ns:key=value  (empty value erases nothing; absence = missing)
    std::string s(env);
    size_t pos = 0;
    while (pos < s.size()) {
        size_t semi = s.find(';', pos);
        if (semi == std::string::npos) semi = s.size();
        std::string item = s.substr(pos, semi - pos);
        pos = semi + 1;
        if (item.empty()) continue;
        size_t eq = item.find('=');
        size_t colon = item.find(':');
        if (eq == std::string::npos || colon == std::string::npos || colon > eq) continue;
        g_nvs_store[item.substr(0, colon)][item.substr(colon + 1, eq - colon - 1)] = item.substr(eq + 1);
    }
    const char* fail = std::getenv("NVS_FAIL_OPEN");
    if (fail != nullptr) {
        // comma-separated namespaces whose open must fail (missing partition)
        std::string f(fail);
        size_t p = 0;
        while (p < f.size()) {
            size_t c = f.find(',', p);
            if (c == std::string::npos) c = f.size();
            g_nvs_open_fail[f.substr(p, c - p)] = true;
            p = c + 1;
        }
    }
}

Settings::Settings(const std::string& ns, bool read_write) : ns_(ns) {
    static bool seeded = false;
    if (!seeded) { seed(); seeded = true; }
    if (g_nvs_open_fail.count(ns)) {
        return;  // handle stays invalid == open failure
    }
}
Settings::~Settings() {}
std::string Settings::GetString(const std::string& key, const std::string& default_value) {
    if (g_nvs_open_fail.count(ns_)) {
        return default_value;  // open failed: reads cannot succeed
    }
    auto ns = g_nvs_store.find(ns_);
    if (ns == g_nvs_store.end()) return default_value;
    auto kv = ns->second.find(key);
    if (kv == ns->second.end()) return default_value;
    return kv->second;
}
void Settings::SetString(const std::string& key, const std::string& value) { g_nvs_store[ns_][key] = value; }
int32_t Settings::GetInt(const std::string& key, int32_t default_value) {
    auto v = GetString(key, "");
    if (v.empty()) return default_value;
    return std::strtol(v.c_str(), nullptr, 10);
}
void Settings::SetInt(const std::string& key, int32_t value) { g_nvs_store[ns_][key] = std::to_string(value); }
bool Settings::GetBool(const std::string& key, bool default_value) {
    auto v = GetString(key, "");
    if (v.empty()) return default_value;
    return v != "0";
}
void Settings::SetBool(const std::string& key, bool value) { g_nvs_store[ns_][key] = value ? "1" : "0"; }
void Settings::EraseKey(const std::string& key) { g_nvs_store[ns_].erase(key); }
void Settings::EraseAll() { g_nvs_store[ns_].clear(); }
"""

MAIN_CC = r"""
#include "tuya_auth.h"
#include <cstdio>
#include <cstring>

int main() {
    TuyaAuthCredentials auth;
    bool ok = TuyaAuthLoad(auth);
    std::printf("RESULT ok=%d uuid=[%s] auth_key=[%s] product_key=[%s]\n",
                ok ? 1 : 0, auth.uuid, auth.auth_key, auth.product_key);
    return ok ? 0 : 1;
}
"""


class TuyaAuthLoaderTest(unittest.TestCase):
    """Compiles main/tuya_auth.cc once, then runs NVS-only scenarios."""

    @classmethod
    def setUpClass(cls):
        cls.workdir = tempfile.mkdtemp(prefix="tuya_auth_loader_")
        d = cls.workdir
        for name, content in (
            ("settings.h", STUB_SETTINGS_H),
            ("nvs_flash.h", STUB_NVS_FLASH_H),
            ("esp_log.h", STUB_ESP_LOG_H),
            ("mock_settings.cc", MOCK_SETTINGS_CC),
            ("main.cc", MAIN_CC),
        ):
            with open(os.path.join(d, name), "w") as f:
                f.write(content)
        exe = os.path.join(d, "tuya_auth_test")
        cmd = [
            "c++", "-std=c++17", "-Wall", "-Werror",
            "-I", d,
            "-I", MAIN_DIR,
            os.path.join(MAIN_DIR, "tuya_auth.cc"),
            os.path.join(d, "mock_settings.cc"),
            os.path.join(d, "main.cc"),
            "-o", exe,
        ]
        subprocess.run(cmd, check=True)
        cls.binary = exe

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.workdir, ignore_errors=True)

    def run_loader(self, nvs_seed=None, fail_open=None):
        env = dict(os.environ)
        env.pop("NVS_SEED_JSON", None)
        env.pop("NVS_FAIL_OPEN", None)
        if nvs_seed:
            env["NVS_SEED_JSON"] = nvs_seed
        if fail_open:
            env["NVS_FAIL_OPEN"] = fail_open
        # Exit code mirrors load success (0/1); assertions inspect stdout.
        proc = subprocess.run(
            [self.binary], env=env,
            capture_output=True, text=True)
        return proc.stdout.strip()

    VALID_SEED = ("tuya_auth:uuid=nvs-uuid-123456789;"
                  "tuya_auth:auth_key=nvs-auth-key-32-bytes-bbbbbbbbxx;"
                  "tuya_auth:product_key=nvspk12345678901")

    def test_loads_from_nvs(self):
        out = self.run_loader(self.VALID_SEED)
        self.assertIn("ok=1", out)
        self.assertIn("uuid=[nvs-uuid-123456789]", out)
        self.assertIn("auth_key=[nvs-auth-key-32-bytes-bbbbbbbbxx]", out)
        self.assertIn("product_key=[nvspk12345678901]", out)

    def test_empty_nvs_fails(self):
        out = self.run_loader()
        self.assertIn("ok=0", out)

    def test_partial_nvs_fails(self):
        out = self.run_loader("tuya_auth:uuid=nvs-uuid-123456789")
        self.assertIn("ok=0", out)

    def test_nvs_partition_open_failure_fails(self):
        # No fallback exists: a missing partition is an error.
        out = self.run_loader(self.VALID_SEED, fail_open="tuya_auth")
        self.assertIn("ok=0", out)

    # --- validation ---

    def test_uuid_too_short_rejected(self):
        # BLE SDK reads 16 fixed bytes from uuid.
        out = self.run_loader(
            "tuya_auth:uuid=short;"
            "tuya_auth:auth_key=nvs-auth-key-32-bytes-bbbbbbbbxx;"
            "tuya_auth:product_key=nvspk12345678901")
        self.assertIn("ok=0", out)

    def test_auth_key_too_short_rejected(self):
        # BLE SDK reads 32 fixed bytes from auth_key.
        out = self.run_loader(
            "tuya_auth:uuid=nvs-uuid-123456789;"
            "tuya_auth:auth_key=tooshort;"
            "tuya_auth:product_key=nvspk12345678901")
        self.assertIn("ok=0", out)

    def test_uuid_31_chars_ok_32_rejected(self):
        seed31 = ("tuya_auth:uuid=" + "u" * 31
                  + ";tuya_auth:auth_key=nvs-auth-key-32-bytes-bbbbbbbbxx"
                  + ";tuya_auth:product_key=nvspk12345678901")
        self.assertIn("ok=1", self.run_loader(seed31))
        seed32 = ("tuya_auth:uuid=" + "u" * 32
                  + ";tuya_auth:auth_key=nvs-auth-key-32-bytes-bbbbbbbbxx"
                  + ";tuya_auth:product_key=nvspk12345678901")
        self.assertIn("ok=0", self.run_loader(seed32))


if __name__ == "__main__":
    unittest.main()
