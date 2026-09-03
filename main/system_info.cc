#include "system_info.h"

#include <freertos/task.h>
#include <esp_log.h>
#include <esp_flash.h>
#include <esp_mac.h>
#include <esp_system.h>
#include <esp_partition.h>
#include <esp_app_desc.h>
#include <esp_ota_ops.h>
#include <esp_pm.h>
#include <esp_heap_caps.h>
#include <lwip/stats.h>
#include <lwip/memp.h>
#if !CONFIG_IDF_TARGET_ESP32P4
#include <esp_wifi.h>
#endif
#if CONFIG_IDF_TARGET_ESP32P4
#include "esp_wifi_remote.h"
#endif

#define TAG "SystemInfo"

size_t SystemInfo::GetFlashSize() {
    uint32_t flash_size;
    if (esp_flash_get_size(NULL, &flash_size) != ESP_OK) {
        ESP_LOGE(TAG, "Failed to get flash size");
        return 0;
    }
    return (size_t)flash_size;
}

size_t SystemInfo::GetMinimumFreeHeapSize() {
    return esp_get_minimum_free_heap_size();
}

size_t SystemInfo::GetFreeHeapSize() {
    return esp_get_free_heap_size();
}

std::string SystemInfo::GetMacAddress() {
    uint8_t mac[6];
#if CONFIG_IDF_TARGET_ESP32P4
    esp_wifi_get_mac(WIFI_IF_STA, mac);
#else
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
#endif
    char mac_str[18];
    snprintf(mac_str, sizeof(mac_str), "%02x:%02x:%02x:%02x:%02x:%02x", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    return std::string(mac_str);
}

std::string SystemInfo::GetChipModelName() {
    return std::string(CONFIG_IDF_TARGET);
}

std::string SystemInfo::GetUserAgent() {
    auto app_desc = esp_app_get_description();
    auto user_agent = std::string(BOARD_NAME "/") + app_desc->version;
    return user_agent;
}

esp_err_t SystemInfo::PrintTaskCpuUsage(TickType_t xTicksToWait) {
    #define ARRAY_SIZE_OFFSET 5
    TaskStatus_t *start_array = NULL, *end_array = NULL;
    UBaseType_t start_array_size, end_array_size;
    configRUN_TIME_COUNTER_TYPE start_run_time, end_run_time;
    esp_err_t ret;
    uint32_t total_elapsed_time;

    //Allocate array to store current task states
    start_array_size = uxTaskGetNumberOfTasks() + ARRAY_SIZE_OFFSET;
    start_array = (TaskStatus_t*)malloc(sizeof(TaskStatus_t) * start_array_size);
    if (start_array == NULL) {
        ret = ESP_ERR_NO_MEM;
        goto exit;
    }
    //Get current task states
    start_array_size = uxTaskGetSystemState(start_array, start_array_size, &start_run_time);
    if (start_array_size == 0) {
        ret = ESP_ERR_INVALID_SIZE;
        goto exit;
    }

    vTaskDelay(xTicksToWait);

    //Allocate array to store tasks states post delay
    end_array_size = uxTaskGetNumberOfTasks() + ARRAY_SIZE_OFFSET;
    end_array = (TaskStatus_t*)malloc(sizeof(TaskStatus_t) * end_array_size);
    if (end_array == NULL) {
        ret = ESP_ERR_NO_MEM;
        goto exit;
    }
    //Get post delay task states
    end_array_size = uxTaskGetSystemState(end_array, end_array_size, &end_run_time);
    if (end_array_size == 0) {
        ret = ESP_ERR_INVALID_SIZE;
        goto exit;
    }

    //Calculate total_elapsed_time in units of run time stats clock period.
    total_elapsed_time = (end_run_time - start_run_time);
    if (total_elapsed_time == 0) {
        ret = ESP_ERR_INVALID_STATE;
        goto exit;
    }

    printf("| Task | Run Time | Percentage\n");
    //Match each task in start_array to those in the end_array
    for (int i = 0; i < start_array_size; i++) {
        int k = -1;
        for (int j = 0; j < end_array_size; j++) {
            if (start_array[i].xHandle == end_array[j].xHandle) {
                k = j;
                //Mark that task have been matched by overwriting their handles
                start_array[i].xHandle = NULL;
                end_array[j].xHandle = NULL;
                break;
            }
        }
        //Check if matching task found
        if (k >= 0) {
            uint32_t task_elapsed_time = end_array[k].ulRunTimeCounter - start_array[i].ulRunTimeCounter;
            uint32_t percentage_time = (task_elapsed_time * 100UL) / (total_elapsed_time * CONFIG_FREERTOS_NUMBER_OF_CORES);
            printf("| %-16s | %8lu | %4lu%%\n", start_array[i].pcTaskName, task_elapsed_time, percentage_time);
        }
    }

    //Print unmatched tasks
    for (int i = 0; i < start_array_size; i++) {
        if (start_array[i].xHandle != NULL) {
            printf("| %s | Deleted\n", start_array[i].pcTaskName);
        }
    }
    for (int i = 0; i < end_array_size; i++) {
        if (end_array[i].xHandle != NULL) {
            printf("| %s | Created\n", end_array[i].pcTaskName);
        }
    }
    ret = ESP_OK;

exit:    //Common return path
    free(start_array);
    free(end_array);
    return ret;
}

void SystemInfo::PrintTaskList() {
    char buffer[1000];
    vTaskList(buffer);
    ESP_LOGI(TAG, "Task list: \n%s", buffer);
}

void SystemInfo::PrintHeapStats() {
    int free_sram = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    int min_free_sram = heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL);
    ESP_LOGI(TAG, "free sram: %u minimal sram: %u", free_sram, min_free_sram);
}

void SystemInfo::PrintPmLocks() {
    esp_pm_dump_locks(stdout);
}

void SystemInfo::DumpWifiStatis() {
#if !CONFIG_IDF_TARGET_ESP32P4
    // Buffer + RX/TX + power-save counters straight from the WiFi driver.
    esp_wifi_statis_dump(WIFI_STATIS_BUFFER | WIFI_STATIS_RXTX | WIFI_STATIS_PS);
#endif
}

void SystemInfo::PrintNetDiag() {
#if !CONFIG_IDF_TARGET_ESP32P4
    int rssi = 0;
    uint8_t chan = 0;
    wifi_ap_record_t ap = {};
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
        rssi = ap.rssi;
        chan = ap.primary;
    }
    wifi_ps_type_t ps = WIFI_PS_NONE;
    esp_wifi_get_ps(&ps);
    uint16_t inactive = 0;
    esp_wifi_get_inactive_time(WIFI_IF_STA, &inactive);

    // WiFi RX buffers come from internal DMA-capable RAM; if this floors, the
    // driver cannot accept frames regardless of what lwIP does.
    size_t dma_free = heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA);
    size_t dma_block = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA);

    ESP_LOGW(TAG, "[NETDIAG] wifi rssi=%d ch=%u ps=%d inactive=%us | dma_free=%u largest=%u",
             rssi, (unsigned)chan, (int)ps, (unsigned)inactive,
             (unsigned)dma_free, (unsigned)dma_block);
#endif

#if LWIP_STATS
    // Deltas, not totals: the layer whose counter stops moving is the culprit.
    // MEM_STATS is unavailable here (lwIP is built with MEM_LIBC_MALLOC=1), so
    // memory pressure shows up as the per-layer memerr counters plus PBUF_POOL.
    static unsigned long p_lr, p_ld, p_lme, p_ir, p_id, p_tr, p_td, p_tme, p_tx, p_trt;
    unsigned long lr  = (unsigned long)lwip_stats.link.recv;
    unsigned long ld  = (unsigned long)lwip_stats.link.drop;
    unsigned long lme = (unsigned long)lwip_stats.link.memerr;
    unsigned long ir  = (unsigned long)lwip_stats.ip.recv;
    unsigned long id  = (unsigned long)lwip_stats.ip.drop;
    unsigned long tr  = (unsigned long)lwip_stats.tcp.recv;
    unsigned long td  = (unsigned long)lwip_stats.tcp.drop;
    unsigned long tme = (unsigned long)lwip_stats.tcp.memerr;
    // xmit vs memerr tells apart "we sent a lot" from "we could not enqueue";
    // rterr rising alongside means the retransmit path is involved too.
    unsigned long tx  = (unsigned long)lwip_stats.tcp.xmit;
    unsigned long trt = (unsigned long)lwip_stats.tcp.rterr;

    ESP_LOGW(TAG, "[NETDIAG] d.link(recv=%lu drop=%lu memerr=%lu) d.ip(recv=%lu drop=%lu) "
                  "d.tcp(recv=%lu xmit=%lu drop=%lu memerr=%lu rterr=%lu)",
             lr - p_lr, ld - p_ld, lme - p_lme, ir - p_ir, id - p_id,
             tr - p_tr, tx - p_tx, td - p_td, tme - p_tme, trt - p_trt);

    p_lr = lr; p_ld = ld; p_lme = lme; p_ir = ir; p_id = id;
    p_tr = tr; p_td = td; p_tme = tme; p_tx = tx; p_trt = trt;

#if MEMP_STATS
    // PBUF_POOL exhaustion is the direct signal for "driver had no buffer".
    const struct stats_mem* pp = lwip_stats.memp[MEMP_PBUF_POOL];
    if (pp != nullptr) {
        ESP_LOGW(TAG, "[NETDIAG] pbuf_pool err=%lu used=%u max=%u avail=%u",
                 (unsigned long)pp->err, (unsigned)pp->used,
                 (unsigned)pp->max, (unsigned)pp->avail);
    }
#endif
#else
    ESP_LOGW(TAG, "[NETDIAG] lwIP stats disabled (set CONFIG_LWIP_STATS=y)");
#endif
}
