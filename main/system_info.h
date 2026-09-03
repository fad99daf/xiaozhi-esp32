#ifndef _SYSTEM_INFO_H_
#define _SYSTEM_INFO_H_

#include <string>

#include <esp_err.h>
#include <freertos/FreeRTOS.h>

class SystemInfo {
public:
    static size_t GetFlashSize();
    static size_t GetMinimumFreeHeapSize();
    static size_t GetFreeHeapSize();
    static std::string GetMacAddress();
    static std::string GetChipModelName();
    static std::string GetUserAgent();
    static esp_err_t PrintTaskCpuUsage(TickType_t xTicksToWait);
    static void PrintTaskList();
    static void PrintHeapStats();
    static void PrintPmLocks();
    // Network-path diagnostics: prints per-layer deltas so a layer that stops
    // advancing (WiFi driver vs lwIP vs TCP) can be identified.
    static void PrintNetDiag();
    // Heavyweight WiFi driver dump; call only when a fault is detected.
    static void DumpWifiStatis();
};

#endif // _SYSTEM_INFO_H_
