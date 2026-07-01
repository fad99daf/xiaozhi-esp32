#include "tuya_protocol.h"
#include "tuya_authkey.h"
#include "settings.h"
#include <cstring>
#include <cstdlib>
#include <esp_log.h>
#include <esp_timer.h>
#include <esp_heap_caps.h>
#include <esp_memory_utils.h>
#include <cJSON.h>
#include <mbedtls/base64.h>

extern "C" {
    extern const pal_t *tai_pal_freertos(void);
}

#define TAG "TuyaProtocol"

// Tuya TTS opus frame parameters (CBR 16kbps, 40ms)
#define TAI_OPUS_FRAME_DURATION_MS 40
#define TAI_OPUS_FRAME_SIZE_BYTES  80

// --- Minimal JSON helpers (matching threaded_chat pattern) ---

static const char *json_find_value(const char *json, const char *key)
{
    if (!json || !key) return nullptr;
    char search[128];
    snprintf(search, sizeof(search), "\"%s\"", key);
    const char *p = strstr(json, search);
    if (!p) return nullptr;
    p += strlen(search);
    while (*p == ' ' || *p == ':' || *p == '\t') p++;
    return p;
}

static int json_get_string(const char *json, const char *key,
                           char *out, size_t cap)
{
    const char *p = json_find_value(json, key);
    if (!p || *p != '"') return -1;
    p++;
    const char *end = strchr(p, '"');
    if (!end) return -1;
    size_t len = (size_t)(end - p);
    if (len >= cap) len = cap - 1;
    memcpy(out, p, len);
    out[len] = '\0';
    return 0;
}

static int json_get_long(const char *json, const char *key, long *out)
{
    const char *p = json_find_value(json, key);
    if (!p) return -1;
    if (*p != '-' && (*p < '0' || *p > '9')) return -1;
    *out = strtol(p, nullptr, 10);
    return 0;
}

static int json_array_first_string(const char *json, const char *key,
                                   char *out, size_t cap)
{
    const char *p = json_find_value(json, key);
    if (!p || *p != '[') return -1;
    p++;
    while (*p == ' ') p++;
    if (*p != '"') return -1;
    p++;
    const char *end = strchr(p, '"');
    if (!end) return -1;
    size_t len = (size_t)(end - p);
    if (len >= cap) len = cap - 1;
    memcpy(out, p, len);
    out[len] = '\0';
    return 0;
}

static char *json_get_object(const char *json, const char *key)
{
    const char *p = json_find_value(json, key);
    if (!p || *p != '{') return nullptr;
    int depth = 0;
    const char *start = p, *q = p;
    while (*q) {
        if (*q == '{') depth++;
        else if (*q == '}' && --depth == 0) {
            size_t len = (size_t)(q - start + 1);
            char *obj = (char *)malloc(len + 1);
            if (obj) { memcpy(obj, start, len); obj[len] = '\0'; }
            return obj;
        }
        q++;
    }
    return nullptr;
}

// --- IoT SDK log callback ---

static void iot_log_cb(log_level_t level, const char *fmt, va_list args)
{
    char buf[256];
    vsnprintf(buf, sizeof(buf), fmt, args);
    switch (level) {
        case LOG_ERROR: ESP_LOGE("IOT", "%s", buf); break;
        case LOG_WARN:  ESP_LOGW("IOT", "%s", buf); break;
        case LOG_INFO:  ESP_LOGI("IOT", "%s", buf); break;
        default:        ESP_LOGD("IOT", "%s", buf); break;
    }
}

static void EnsureSdkInitialized() {
    static bool initialized = false;
    if (initialized) return;
    log_set_handler(iot_log_cb);
    iot_init(tai_pal_freertos());
    initialized = true;
}

// --- TuyaProtocol implementation ---

TuyaProtocol::TuyaProtocol() {
}

TuyaProtocol::~TuyaProtocol() {
    if (ctx_) {
        tai_disconnect(ctx_);
        tai_ctx_deinit(ctx_);
    }
    if (ctx_mem_) free(ctx_mem_);
    if (token_) free(token_);
    if (iot_client_) iot_client_deinit(iot_client_);
}

bool TuyaProtocol::InitIotClient() {
    EnsureSdkInitialized();

    Settings tuya_nvs("tuya", false);
    std::string nvs_devid = tuya_nvs.GetString("devid");

    if (!nvs_devid.empty()) {
        // Previously on-boarded — use NVS credentials
        std::string nvs_secret = tuya_nvs.GetString("secret_key");
        std::string nvs_local = tuya_nvs.GetString("local_key");

        iot_client_config_t cfg = {};
        strncpy((char*)cfg.devid, nvs_devid.c_str(), sizeof(cfg.devid) - 1);
        strncpy((char*)cfg.secret_key, nvs_secret.c_str(), sizeof(cfg.secret_key) - 1);
        strncpy((char*)cfg.local_key, nvs_local.c_str(), sizeof(cfg.local_key) - 1);
        cfg.region = AY;
        cfg.env = PROD;
        cfg.mqtt_disable_tls = false;

        iot_client_ = iot_client_init(&cfg);
        if (iot_client_) {
            ESP_LOGI(TAG, "IoT client initialized from NVS (devid=%s)", nvs_devid.c_str());
            return true;
        }
        ESP_LOGW(TAG, "NVS credentials failed, trying on-boarding...");
    }

    ESP_LOGE(TAG, "No credentials available - device not activated");
    return false;
}

bool TuyaProtocol::OnBoardWithToken(const std::string& token) {
    ESP_LOGI(TAG, "On-boarding with BLE token: %s", token.c_str());

    EnsureSdkInitialized();

    iot_on_boarding_config_t cfg = {};
    memcpy((char*)cfg.uuid, TUYA_UUID, strlen(TUYA_UUID));
    memcpy((char*)cfg.authkey, TUYA_AUTH_KEY, strlen(TUYA_AUTH_KEY));
    memcpy((char*)cfg.product_key, TUYA_PRODUCT_KEY, strlen(TUYA_PRODUCT_KEY));
    cfg.timeout_ms = 30000;
    cfg.env = PROD;
    cfg.mqtt_disable_tls = false;
    cfg.mqtt_auto_connect = true;

    iot_client_t* client = iot_client_init_on_boarding_with_token(&cfg, token.c_str());
    if (!client) {
        ESP_LOGE(TAG, "On-boarding with BLE token failed");
        return false;
    }

    // Persist on-boarded credentials to NVS for subsequent boots
    {
        Settings settings("tuya", true);
        settings.SetString("devid", client->devid);
        settings.SetString("secret_key", client->secret_key);
        settings.SetString("local_key", client->local_key);
    }

    ESP_LOGI(TAG, "On-boarded successfully, devid=%s", client->devid);

    // don't Free the client —  the mqtt is used by data point management
    //iot_client_deinit(client);
    return true;
}

bool TuyaProtocol::FetchToken() {
    token_ = (char *)calloc(1, 4096);
    if (!token_) return false;

    int ret = iot_client_get_session_token(iot_client_, nullptr, token_, 4096);
    if (ret != 0 || token_[0] == '\0') {
        ESP_LOGE(TAG, "iot_client_get_session_token failed: %d", ret);
        free(token_);
        token_ = nullptr;
        return false;
    }
    ESP_LOGI(TAG, "Session token acquired (len=%d)", (int)strlen(token_));
    return true;
}

bool TuyaProtocol::ParseToken() {
    memset(&conn_params_, 0, sizeof(conn_params_));

    // Try base64 decode
    char *json = nullptr;
    size_t token_len = strlen(token_);
    size_t decoded_cap = token_len;  // base64 decoded is always smaller
    char *decoded = (char *)malloc(decoded_cap + 1);
    if (decoded) {
        size_t olen = 0;
        int ret = mbedtls_base64_decode((unsigned char *)decoded, decoded_cap,
                                        &olen,
                                        (const unsigned char *)token_, token_len);
        if (ret == 0 && olen > 0 && decoded[0] == '{') {
            decoded[olen] = '\0';
            json = decoded;
        } else {
            free(decoded);
            json = strdup(token_);
        }
    } else {
        json = strdup(token_);
    }

    if (!json) return false;

    char *conn = json_get_object(json, "connect_conf");
    if (!conn) {
        ESP_LOGE(TAG, "'connect_conf' not found in token");
        free(json);
        return false;
    }

    json_array_first_string(conn, "hosts", conn_params_.host, sizeof(conn_params_.host));

    if (json_array_first_string(conn, "domains", conn_params_.tls_sni, sizeof(conn_params_.tls_sni)) != 0)
        strncpy(conn_params_.tls_sni, conn_params_.host, sizeof(conn_params_.tls_sni) - 1);

    long port = 0;
    if (json_get_long(conn, "ecc_tls_port", &port) != 0)
        json_get_long(conn, "tcpport", &port);
    conn_params_.port = (port > 0) ? (uint16_t)port : 443;

    json_get_string(conn, "derived_client_id",
                    conn_params_.derived_client_id, sizeof(conn_params_.derived_client_id));
    free(conn);

    char *sess = json_get_object(json, "session_conf");
    if (sess) {
        json_get_string(sess, "agentToken",
                        conn_params_.agent_token, sizeof(conn_params_.agent_token));
        char *biz = json_get_object(sess, "bizConfig");
        if (biz) {
            json_get_long(biz, "bizCode", &conn_params_.biz_code);
            json_get_long(biz, "bizTag", &conn_params_.biz_tag);
            free(biz);
        }
        free(sess);
    }

    free(json);

    if (conn_params_.host[0] == '\0') {
        ESP_LOGE(TAG, "Could not extract host from token");
        return false;
    }

    if (conn_params_.biz_code == 0) conn_params_.biz_code = 65537;
    if (conn_params_.biz_tag == 0) conn_params_.biz_tag = 119;

    ESP_LOGI(TAG, "TAI server: %s:%u (SNI: %s)", conn_params_.host,
             conn_params_.port, conn_params_.tls_sni);
    return true;
}

bool TuyaProtocol::BuildTaiContext() {
    size_t sz = tai_ctx_size();
    // Allocate the 80 KB TAI context in PSRAM rather than internal RAM.
    // The TAI PAL accesses this memory only from regular FreeRTOS tasks
    // (no ISR / cache-disabled paths), so PSRAM is safe and saves ~80 KB
    // of internal DRAM.
    ctx_mem_ = heap_caps_malloc(sz, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!ctx_mem_) {
        ESP_LOGE(TAG, "Failed to allocate %u bytes for TAI context in PSRAM", (unsigned)sz);
        return false;
    }

    tai_config_t cfg = {};
    cfg.host = conn_params_.host;
    cfg.port = conn_params_.port;
    cfg.tls_sni = conn_params_.tls_sni;
    cfg.device_id = conn_params_.derived_client_id;
    cfg.local_key = local_key_.c_str();
    cfg.client_type = TAI_CLIENT_DEVICE;
    cfg.protocol_version = TAI_VER_21;
    cfg.biz_code = (uint32_t)conn_params_.biz_code;
    cfg.biz_tag = (uint64_t)conn_params_.biz_tag;
    cfg.sign_level = TAI_SIGN_HMAC_SHA256;
    cfg.agent_token = conn_params_.agent_token;
    cfg.pal = tai_pal_freertos();

    cfg.session_attrs_json =
        "{\"deviceMcp\":{\"supportCustomMCP\":true},"
        "\"tts.order.supports\":[{\"format\":\"opus\","
        "\"sampleRate\":16000,\"bitDepth\":\"16\",\"channels\":1}]}";

    cfg.event_user_data_json =
        "{\"sys.workflow\":\"asr-llm-tts\","
        "\"asr.enableVad\":true,"
        "\"tts.alternate\":true,"
        "\"processing.interrupt\":true}";

    cfg.on_audio = OnAudioCb;
    cfg.on_text = OnTextCb;
    cfg.on_event = OnEventCb;
    cfg.on_disconnect = OnDisconnectCb;
    cfg.user_data = this;

    ctx_ = tai_ctx_init(ctx_mem_, &cfg);
    if (!ctx_) {
        ESP_LOGE(TAG, "tai_ctx_init failed");
        free(ctx_mem_);
        ctx_mem_ = nullptr;
        return false;
    }

    ESP_LOGI(TAG, "TAI context built (%uKB in %s)", (unsigned)(sz / 1024),
             esp_ptr_external_ram(ctx_mem_) ? "PSRAM" : "internal");

    /* Enable agentic-kit debug logging (level 4 = DEBUG).
     * This logs t_send(), t_recv() entries, packet dispatch, etc.
     * at the agentic-kit layer using the project-wide log facade. */
    tai_set_log_level(2);

    return true;
}

bool TuyaProtocol::RefreshTaiContext() {
    if (ctx_) {
        tai_ctx_deinit(ctx_);
        ctx_ = nullptr;
    }
    if (ctx_mem_) {
        free(ctx_mem_);
        ctx_mem_ = nullptr;
    }
    if (token_) {
        free(token_);
        token_ = nullptr;
    }

    if (!InitIotClient()) return false;
    local_key_ = iot_client_->local_key;
    if (!FetchToken()) return false;

    iot_client_deinit(iot_client_);
    iot_client_ = nullptr;

    if (!ParseToken()) return false;
    if (!BuildTaiContext()) return false;
    return true;
}

static void log_heap_delta(const char *label, size_t before_internal, size_t before_psram) {
    size_t after_internal = heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    size_t after_psram = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    int delta_int = (int)before_internal - (int)after_internal;
    int delta_ps = (int)before_psram - (int)after_psram;
    ESP_LOGW("HEAP_TRACE", "[%s] internal: %+d bytes, PSRAM: %+d bytes (free: int=%u, ps=%u)",
             label, delta_int, delta_ps,
             (unsigned)after_internal, (unsigned)after_psram);
}

bool TuyaProtocol::Start() {
    size_t start_internal = heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    size_t start_psram = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    ESP_LOGW("HEAP_TRACE", "[Start BEGIN] free internal=%u, PSRAM=%u",
             (unsigned)start_internal, (unsigned)start_psram);

    size_t before_int, before_ps;

    before_int = heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    before_ps = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    if (!InitIotClient()) return false;
    log_heap_delta("InitIotClient", before_int, before_ps);

    local_key_ = iot_client_->local_key;

    before_int = heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    before_ps = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    if (!FetchToken()) return false;
    log_heap_delta("FetchToken", before_int, before_ps);

    // Disconnect IoT MQTT client to free ~30KB of internal TLS buffers.
    // The client is only needed to fetch the session token; the TAI audio
    // channel uses its own TLS connection.
    before_int = heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    before_ps = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    iot_client_deinit(iot_client_);
    iot_client_ = nullptr;
    log_heap_delta("iot_client_deinit (freed)", before_int, before_ps);

    if (!ParseToken()) return false;

    before_int = heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    before_ps = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    if (!BuildTaiContext()) return false;
    log_heap_delta("BuildTaiContext", before_int, before_ps);

    server_sample_rate_ = 16000;
    server_frame_duration_ = 40;

    log_heap_delta("Start TOTAL", start_internal, start_psram);
    ESP_LOGW("HEAP_TRACE", "[Start END] free internal=%u, PSRAM=%u",
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    return true;
}

bool TuyaProtocol::OpenAudioChannel() {
    if (session_active_) return true;

    if (connect_fail_count_ >= MAX_CONNECT_FAILS_BEFORE_REFRESH) {
        ESP_LOGW(TAG, "Refreshing TAI context after %d consecutive failures", connect_fail_count_);
        if (!RefreshTaiContext()) {
            SetError("Tuya AI refresh failed");
            return false;
        }
        connect_fail_count_ = 0;
    }

    size_t before_int = heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    size_t before_ps = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    int rc = tai_connect(ctx_);
    if (rc != TAI_OK) {
        connect_fail_count_++;
        ESP_LOGE(TAG, "tai_connect failed: %d (attempt %d)", rc, connect_fail_count_);
        SetError("Tuya AI connect failed");
        return false;
    }
    log_heap_delta("tai_connect (TLS)", before_int, before_ps);
    size_t min_int = heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    ESP_LOGW("HEAP_TRACE", "[tai_connect peak] min internal ever=%u (peak used during handshake=~%d)",
             (unsigned)min_int,
             (int)before_int - (int)min_int);

    connect_fail_count_ = 0;
    connected_ = true;
    session_active_ = true;
    is_first_audio_packet_ = true;
    has_received_first_nlg_ = false;
    audio_recv_count_ = 0;
    audio_reassembly_buf_.clear();

    ESP_LOGI(TAG, "Audio channel opened");
    if (on_audio_channel_opened_) on_audio_channel_opened_();
    return true;
}

void TuyaProtocol::CloseAudioChannel(bool send_goodbye) {
    if (!session_active_) return;

    tai_disconnect(ctx_);
    connected_ = false;
    session_active_ = false;

    ESP_LOGI(TAG, "Audio channel closed");
    if (on_audio_channel_closed_) on_audio_channel_closed_();
}

bool TuyaProtocol::IsAudioChannelOpened() const {
    return session_active_;
}

void TuyaProtocol::SendStartListening(ListeningMode mode) {
    //ESP_LOGI(TAG, "[BARGE-IN] SendStartListening: mode=%d, is_first=%d", mode, (int)is_first_audio_packet_);
    if (!ctx_ || !session_active_) {
        ESP_LOGW(TAG, "SendStartListening: not ready");
        return;
    }
    has_received_first_nlg_ = false;
    audio_end_pending_ = false;

    std::lock_guard<std::mutex> lock(send_mutex_);
    int rc = tai_send_audio_start(ctx_, TAI_AUDIO_OPUS, 1, 16, 16000);
    ESP_LOGI(TAG, "tai_send_audio_start rc=%d", rc);
    is_first_audio_packet_ = false;
}

bool TuyaProtocol::SendAudio(std::unique_ptr<AudioStreamPacket> packet) {
    if (!ctx_ || !session_active_) {
        ESP_LOGW(TAG, "[TRACE] SendAudio: not ready (ctx=%p, active=%d)",
                 ctx_, session_active_.load());
        return false;
    }

    /* Log every 100th packet to confirm audio is flowing during speaking state
     * (this is critical for server-side barge-in).  Using ESP_LOGI rather than
     * ESP_LOGD so the logs are always visible regardless of component log level. */
    static uint32_t send_seq = 0;
    static int64_t first_ts = 0;
    uint32_t seq = send_seq++;
    if (seq % 100 == 0) {
        int64_t now = esp_timer_get_time() / 1000;  // ms
        if (first_ts == 0) first_ts = now;
        //float rate = (now - first_ts) > 0 ? (seq + 1) * 1000.0f / (now - first_ts) : 0;
        //ESP_LOGI(TAG, "[BARGE-IN] Audio send #%lu: %d bytes @ %.1f pkt/s (elapsed=%lld ms)",
        //         (unsigned long)seq, (int)packet->payload.size(), rate, now - first_ts);
    }

    std::lock_guard<std::mutex> lock(send_mutex_);
    int rc = tai_send_audio_chunk(ctx_, packet->payload.data(), packet->payload.size());
    if (rc != TAI_OK) {
        ESP_LOGE(TAG, "tai_send_audio_chunk failed: %d (len=%d)", rc, (int)packet->payload.size());
    }
    return rc == TAI_OK;
}

void TuyaProtocol::SendStopListening() {
    if (!ctx_ || !session_active_) return;
    std::lock_guard<std::mutex> lock(send_mutex_);
    ESP_LOGW(TAG, "[BARGE-IN] tai_send_audio_end (stopping audio stream)");
    tai_send_audio_end(ctx_);
    is_first_audio_packet_ = true;
}

void TuyaProtocol::SendAbortSpeaking(AbortReason reason) {
    if (!ctx_ || !session_active_) return;
    ESP_LOGW(TAG, "[BARGE-IN] Sending tai_chat_break (reason=%d)", reason);
    std::lock_guard<std::mutex> lock(send_mutex_);
    tai_chat_break(ctx_);
}

void TuyaProtocol::SendMcpMessage(const std::string& payload) {
    if (!ctx_ || !session_active_) return;
    std::lock_guard<std::mutex> lock(send_mutex_);
    tai_send_mcp_response(ctx_, payload.c_str());
}

bool TuyaProtocol::SendText(const std::string& text) {
    if (!ctx_ || !session_active_) return false;
    std::lock_guard<std::mutex> lock(send_mutex_);
    int rc = tai_send_text(ctx_, text.c_str(), text.size());
    return rc == TAI_OK;
}

// --- SDK callbacks ---

void TuyaProtocol::OnAudioCb(tai_ctx_t* ctx, const tai_audio_msg_t* msg,
                              void* user) {
    auto self = static_cast<TuyaProtocol*>(user);
    self->HandleAudio(msg->data, msg->len, msg->sample_rate,
                      msg->frame_duration);
}

void TuyaProtocol::OnTextCb(tai_ctx_t* ctx, const tai_text_msg_t* msg,
                             void* user) {
    auto self = static_cast<TuyaProtocol*>(user);
    self->HandleText(msg->text, msg->len, msg->stream_flag);
}

void TuyaProtocol::OnEventCb(tai_ctx_t* ctx, const tai_event_msg_t* msg,
                              void* user) {
    auto self = static_cast<TuyaProtocol*>(user);
    self->HandleEvent(msg->event_type, msg->data, msg->len);
}

void TuyaProtocol::OnDisconnectCb(tai_ctx_t* ctx,
                                   const tai_disconnect_msg_t* msg,
                                   void* user) {
    auto self = static_cast<TuyaProtocol*>(user);
    self->HandleDisconnect(msg->close_code);
}

void TuyaProtocol::HandleAudio(const uint8_t* data, size_t len,
                                uint32_t sample_rate, uint16_t frame_duration) {
    audio_recv_count_++;
    if (audio_recv_count_ % 50 == 1) {
        //ESP_LOGI(TAG, "[BARGE-IN] HandleAudio #%d: len=%d, sr=%u, fd=%u",
        //         audio_recv_count_, (int)len, sample_rate, frame_duration);
    }
    if (!on_incoming_audio_ || len == 0) return;

    if (sample_rate > 0) server_sample_rate_ = (int)sample_rate;
    if (frame_duration > 0) {
        server_frame_duration_ = (int)frame_duration;
    } else if (server_frame_duration_ == 0) {
        server_frame_duration_ = TAI_OPUS_FRAME_DURATION_MS;
    }

    // Tuya TTS sends raw concatenated CBR opus frames (no framing header).
    // TCP chunking may split frames across packets, so we reassemble here.
    const size_t frame_size = TAI_OPUS_FRAME_SIZE_BYTES;

    // Append incoming data to reassembly buffer
    audio_reassembly_buf_.insert(audio_reassembly_buf_.end(), data, data + len);

    // Extract complete frames
    size_t offset = 0;
    while (offset + frame_size <= audio_reassembly_buf_.size()) {
        auto pkt = std::make_unique<AudioStreamPacket>();
        pkt->payload.assign(audio_reassembly_buf_.begin() + offset,
                            audio_reassembly_buf_.begin() + offset + frame_size);
        pkt->sample_rate = server_sample_rate_;
        pkt->frame_duration = server_frame_duration_;
        on_incoming_audio_(std::move(pkt));
        offset += frame_size;
    }

    // Keep leftover bytes for next packet
    if (offset > 0) {
        audio_reassembly_buf_.erase(audio_reassembly_buf_.begin(),
                                    audio_reassembly_buf_.begin() + offset);
    }
}

void TuyaProtocol::HandleText(const char* text, size_t len, uint8_t stream_flag) {
    //ESP_LOGI(TAG, "HandleText: flag=%d len=%d text=%.*s", stream_flag, (int)len,
    //         (int)(len > 200 ? 200 : len), text);
    if (!on_incoming_json_) return;
    std::string raw(text, len);

    cJSON* root = cJSON_Parse(raw.c_str());
    if (!root) {
        ESP_LOGW(TAG, "HandleText: JSON parse failed");
        return;
    }

    cJSON* bizType = cJSON_GetObjectItem(root, "bizType");
    cJSON* dataObj = cJSON_GetObjectItem(root, "data");
    if (!cJSON_IsString(bizType) || !cJSON_IsObject(dataObj)) {
        ESP_LOGW(TAG, "HandleText: missing bizType or data, keys:");
        cJSON* item = root->child;
        while (item) {
            ESP_LOGW(TAG, "  key: %s", item->string ? item->string : "(null)");
            item = item->next;
        }
        cJSON_Delete(root);
        return;
    }

    if (strcmp(bizType->valuestring, "ASR") == 0) {
        cJSON* t = cJSON_GetObjectItem(dataObj, "text");
        if (cJSON_IsString(t) && strlen(t->valuestring) > 0) {
            cJSON* out = cJSON_CreateObject();
            cJSON_AddStringToObject(out, "type", "stt");
            cJSON_AddStringToObject(out, "text", t->valuestring);
            on_incoming_json_(out);
            cJSON_Delete(out);
        }
    } else if (strcmp(bizType->valuestring, "NLG") == 0) {
        if (!has_received_first_nlg_) {
            cJSON* start = cJSON_CreateObject();
            cJSON_AddStringToObject(start, "type", "tts");
            cJSON_AddStringToObject(start, "state", "start");
            on_incoming_json_(start);
            cJSON_Delete(start);
            has_received_first_nlg_ = true;
        }
        cJSON* content = cJSON_GetObjectItem(dataObj, "content");
        if (cJSON_IsString(content) && strlen(content->valuestring) > 0) {
            cJSON* out = cJSON_CreateObject();
            cJSON_AddStringToObject(out, "type", "tts");
            cJSON_AddStringToObject(out, "state", "sentence_start");
            cJSON_AddStringToObject(out, "text", content->valuestring);
            on_incoming_json_(out);
            cJSON_Delete(out);
        }
    }
    cJSON_Delete(root);
}

void TuyaProtocol::HandleEvent(uint16_t event_type,
                                const uint8_t* data, size_t len) {
    //ESP_LOGI(TAG, "HandleEvent: type=%u len=%d", event_type, (int)len);

    if (event_type == TAI_EVT_SERVER_VAD) {
        ESP_LOGW(TAG, "[BARGE-IN] Server VAD detected end of speech (is_first=%d)", (int)is_first_audio_packet_);
        is_first_audio_packet_ = true;
        audio_end_pending_ = true;
        if (on_incoming_json_) {
            cJSON* out = cJSON_CreateObject();
            cJSON_AddStringToObject(out, "type", "vad");
            cJSON_AddStringToObject(out, "state", "stop");
            on_incoming_json_(out);
            cJSON_Delete(out);
        }
    } else if (event_type == TAI_EVT_CHAT_BREAK) {
        ESP_LOGW(TAG, "[BARGE-IN] *** CHAT_BREAK received from server! Aborting TTS. ***");
        if (on_incoming_json_) {
            cJSON* out = cJSON_CreateObject();
            cJSON_AddStringToObject(out, "type", "tts");
            cJSON_AddStringToObject(out, "state", "abort");
            on_incoming_json_(out);
            cJSON_Delete(out);
        }
        has_received_first_nlg_ = false;
    } else if (event_type == TAI_EVT_END) {
        if (has_received_first_nlg_ && on_incoming_json_) {
            cJSON* out = cJSON_CreateObject();
            cJSON_AddStringToObject(out, "type", "tts");
            cJSON_AddStringToObject(out, "state", "stop");
            on_incoming_json_(out);
            cJSON_Delete(out);
        }
        has_received_first_nlg_ = false;
    } else if (event_type == TAI_EVT_MCP_CMD && data && len > 0) {
        if (on_incoming_json_) {
            std::string payload((const char*)data, len);
            cJSON* root = cJSON_CreateObject();
            cJSON_AddStringToObject(root, "type", "mcp");
            cJSON* pj = cJSON_Parse(payload.c_str());
            if (pj) cJSON_AddItemToObject(root, "payload", pj);
            on_incoming_json_(root);
            cJSON_Delete(root);
        }
    }
}

void TuyaProtocol::HandleDisconnect(uint16_t error_code) {
    connected_ = false;
    session_active_ = false;
    ESP_LOGW(TAG, "Disconnected (code=%u)", error_code);
    if (on_network_error_) {
        on_network_error_("Tuya AI connection lost");
    }
}
