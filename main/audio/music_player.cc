#include "music_player.h"
#include "music_http_resume.h"
#include "music_diagnostics.h"

#include <algorithm>
#include <cstring>
#include <ctime>

#include <esp_crt_bundle.h>
#include <esp_http_client.h>
#include <esp_log.h>
#include <esp_heap_caps.h>
#include <esp_timer.h>
#include <freertos/idf_additions.h>
#include <cJSON.h>
#include <decoder/impl/esp_mp3_dec.h>
#include <simple_dec/esp_audio_simple_dec.h>

#define TAG "MusicPlayer"
#define MUSIC_MAX_URLS 16
#define MUSIC_MAX_URL_LENGTH 2048
#define MUSIC_HTTP_READ_SIZE 4096
#define MUSIC_ENCODED_BUFFER_LIMIT (64 * 1024)
#define MUSIC_DECODED_BUFFER_LIMIT 16384
#define MUSIC_HTTP_MAX_RECONNECTS 3

static const cJSON* GetStringField(const cJSON* object, const char* name) {
    const cJSON* value = cJSON_GetObjectItem(object, name);
    return cJSON_IsString(value) && value->valuestring ? value : nullptr;
}

static bool IsHttpUrl(const char* url) {
    return url && (strncmp(url, "https://", 8) == 0 || strncmp(url, "http://", 7) == 0);
}

static const cJSON* SelectSkillContainer(const cJSON* root) {
    const cJSON* custom = cJSON_GetObjectItem(root, "custom");
    if (cJSON_IsObject(custom) && GetStringField(custom, "action")) return custom;
    const cJSON* general = cJSON_GetObjectItem(root, "general");
    if (cJSON_IsObject(general) && GetStringField(general, "action")) return general;
    return nullptr;
}

static void LogMusicMetadata(const char* source, const cJSON* item) {
    auto text = [&](const char* key) {
        const cJSON* field = GetStringField(item, key);
        return MusicLogText(field ? field->valuestring : nullptr);
    };
    auto number = [&](const char* key) {
        const cJSON* field = cJSON_GetObjectItem(item, key);
        return cJSON_IsNumber(field) ? field->valuedouble : -1.0;
    };
    const cJSON* url = GetStringField(item, "url");
    ESP_LOGD(TAG, "Music metadata: source=%s audioId=%s name=%s channel=%s resource=%08x "
             "durationMs=%.0f duration_raw=%.0f bitrate=%.0f ttlSeconds=%.0f expiresAt=%.0f success=%d errorCode=%s",
             source, text("audioId").c_str(), text("name").c_str(), text("channelCode").c_str(),
             (unsigned)(url ? MusicResourceId(url->valuestring) : 0),
             number("durationMs"), number("duration"), number("bitrate"), number("ttlSeconds"),
             number("playUrlExpiresAt"),
             cJSON_IsBool(cJSON_GetObjectItem(item, "success")) ?
                 (cJSON_IsTrue(cJSON_GetObjectItem(item, "success")) ? 1 : 0) : -1,
             text("errorCode").c_str());
}

MusicPlayer::MusicPlayer(AudioCodec* codec, BeginPlayback begin_playback, PcmSink pcm_sink,
                         WaitForOutput wait_for_output, CancelOutput cancel_output,
                         PauseOutput pause_output, ResumeOutput resume_output,
                         CloudRequestReady cloud_request_ready, bool auto_next_enabled)
    : codec_(codec), begin_playback_(std::move(begin_playback)), pcm_sink_(std::move(pcm_sink)),
      wait_for_output_(std::move(wait_for_output)), cancel_output_(std::move(cancel_output)),
      pause_output_(std::move(pause_output)), resume_output_(std::move(resume_output)),
      cloud_request_ready_(std::move(cloud_request_ready)), auto_next_enabled_(auto_next_enabled) {
    task_stopped_ = xSemaphoreCreateBinaryStatic(&task_stopped_storage_);
}

MusicPlayer::~MusicPlayer() {
    Shutdown();
}

bool MusicPlayer::Start() {
    if (running_.load()) return true;
    running_ = true;
    if (xTaskCreateWithCaps(&MusicPlayer::TaskEntry, "music_player", 8192, this, 3,
                            &task_, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) != pdPASS) {
        running_ = false;
        task_ = nullptr;
        ESP_LOGE(TAG, "Failed to create music playback task");
        return false;
    }
    ESP_LOGI(TAG, "Music worker started (PSRAM stack, free internal=%u)",
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    return true;
}

void MusicPlayer::CancelCloudRequestLocked() {
    auto_next_ready_ = false;
    auto_next_deadline_ms_ = 0;
    catalog_.CancelPending();
}

void MusicPlayer::Shutdown() {
    if (!running_.exchange(false)) return;
    {
        std::lock_guard<std::mutex> lock(request_mutex_);
        ESP_LOGI(TAG, "Music control: gen=%u reason=shutdown", (unsigned)request_generation_.load());
        request_generation_.fetch_add(1);
        CancelCloudRequestLocked();
        pending_urls_.clear();
        playback_state_.Stop();
    }
    if (cancel_output_) cancel_output_();
    if (task_) {
        xTaskNotifyGive(task_);
        xSemaphoreTake(task_stopped_, portMAX_DELAY);
        vTaskDeleteWithCaps(task_);
        task_ = nullptr;
    }
}

bool MusicPlayer::HandleSkillCard(const cJSON* root) {
    if (!cJSON_IsObject(root)) return false;
    const cJSON* code_item = GetStringField(root, "code");
    const char* code = code_item ? code_item->valuestring : "";
    if (strcmp(code, "music") != 0 && strcmp(code, "story") != 0 && strcmp(code, "PlayControl") != 0) {
        return false;
    }

    const cJSON* container = SelectSkillContainer(root);
    const cJSON* action_item = container ? GetStringField(container, "action") : nullptr;
    const char* action = action_item ? action_item->valuestring : "";
    const cJSON* data = container ? cJSON_GetObjectItem(container, "data") : nullptr;
    if (strcmp(code, "PlayControl") == 0 &&
        (strcmp(action, "music_list") == 0 || strcmp(action, "refresh_play_url") == 0)) {
        return HandleCloudCard(action, data);
    }
    if (strcmp(action, "stop") == 0) {
        // Tuya PlayControl uses "stop" for pause. Keep the active stream and
        // queued PCM so a later resume card (without URLs) can continue it.
        std::lock_guard<std::mutex> lock(request_mutex_);
        CancelCloudRequestLocked();
        if (playback_state_.PauseForTurn()) {
            if (pause_output_) pause_output_();
            ESP_LOGI(TAG, "Music paused; stream retained for resume (gen=%u reason=skill_stop)",
                     (unsigned)request_generation_.load());
        } else {
            // A not-yet-started playlist has no playback position to retain.
            request_generation_.fetch_add(1);
            pending_urls_.clear();
            pending_ticket_ = gate_.Arm(false);
            if (cancel_output_) cancel_output_();
            if (task_) xTaskNotifyGive(task_);
        }
        return true;
    }

    const cJSON* pre_tts = cJSON_IsObject(data) ? cJSON_GetObjectItem(data, "preTtsFlag") : nullptr;
    const bool wait_for_tts = cJSON_IsTrue(pre_tts);
    if (strcmp(action, "resume") == 0) {
        std::lock_guard<std::mutex> lock(request_mutex_);
        if (!playback_state_.RequestResume(gate_, wait_for_tts)) {
            ESP_LOGW(TAG, "Cannot resume: no interrupted music stream");
            return false;
        }
        if (playback_state_.OnTtsFinished(gate_) && resume_output_) resume_output_();
        ESP_LOGI(TAG, "Music resume %s (gen=%u)", playback_state_.paused() ? "awaiting TTS" : "ready",
                 (unsigned)request_generation_.load());
        return true;
    }

    const cJSON* audios = cJSON_IsObject(data) ? cJSON_GetObjectItem(data, "audios") : nullptr;
    if ((strcmp(action, "play") != 0 && strcmp(action, "next") != 0 && strcmp(action, "prev") != 0) ||
        !cJSON_IsArray(audios)) {
        ESP_LOGW(TAG, "Ignoring music card without a playable audio list (action=%s)", action);
        return false;
    }

    std::vector<std::string> urls;
    const int count = std::min(cJSON_GetArraySize(audios), MUSIC_MAX_URLS);
    for (int i = 0; i < count; ++i) {
        const cJSON* item = cJSON_GetArrayItem(audios, i);
        const cJSON* url = GetStringField(item, "url");
        const cJSON* format = GetStringField(item, "format");
        if (!url || !format || strcmp(format->valuestring, "mp3") != 0 ||
            !IsHttpUrl(url->valuestring) || strlen(url->valuestring) >= MUSIC_MAX_URL_LENGTH) {
            ESP_LOGW(TAG, "Skipping unsupported music item at index %d", i);
            continue;
        }
        urls.emplace_back(url->valuestring);
        LogMusicMetadata(code, item);
    }

    if (urls.empty()) {
        ESP_LOGW(TAG, "Music skill card contains no supported MP3 URL");
        return false;
    }

    if (!Start()) return false;
    const size_t track_count = urls.size();
    {
        std::lock_guard<std::mutex> lock(request_mutex_);
        // Supersede only the previous music PCM, never the shared TTS queue.
        ESP_LOGI(TAG, "Music control: gen=%u reason=new_playlist action=%s", (unsigned)request_generation_.load(), action);
        playback_state_.Stop();
        if (cancel_output_) cancel_output_();
        pending_urls_ = std::move(urls);
        music_playlist_ = strcmp(code, "music") == 0 ||
                          (strcmp(code, "PlayControl") == 0 && music_playlist_);
        catalog_.Reset();
        auto_next_ready_ = false;
        auto_next_deadline_ms_ = 0;
        pending_ticket_ = gate_.Arm(wait_for_tts);
        request_generation_.fetch_add(1);
    }
    xTaskNotifyGive(task_);
    ESP_LOGI(TAG, "Queued music playlist (%u tracks)", (unsigned)track_count);
    return true;
}

bool MusicPlayer::HandleCloudCard(const char* action, const cJSON* data) {
    uint32_t generation = 0;
    bool request_more = false, queued = false;
    {
        std::lock_guard<std::mutex> lock(request_mutex_);
        const uint64_t now_ms = esp_timer_get_time() / 1000;
        if (!running_.load() || !auto_next_enabled_ || !auto_next_deadline_ms_) return false;
        if (now_ms >= auto_next_deadline_ms_) {
            CancelCloudRequestLocked();
            return false;
        }
        std::string url;
        const auto result = catalog_.HandleResponse(action, data, url);
        if (result == MusicCatalog::Result::Ignored) return false;
        // Log only accepted responses, and bound diagnostic work per page.
        const cJSON* items = cJSON_GetObjectItem(data, "items");
        const cJSON* item = nullptr;
        int logged = 0;
        cJSON_ArrayForEach(item, items) {
            if (logged++ >= 8) break;
            LogMusicMetadata(action, item);
        }
        auto_next_deadline_ms_ = 0;
        auto_next_ready_ = false;
        if (result == MusicCatalog::Result::NeedRequest) {
            auto_next_ready_ = true;
            request_more = true;
            generation = request_generation_.load();
            ESP_LOGI(TAG, "Music catalog page accepted; resolving the next local track URL");
        } else if (result == MusicCatalog::Result::Playable) {
            playback_state_.Stop();
            if (cancel_output_) cancel_output_();
            pending_urls_ = {std::move(url)};
            music_playlist_ = true;
            pending_ticket_ = gate_.Arm(false);
            request_generation_.fetch_add(1);
            queued = true;
            ESP_LOGI(TAG, "Music catalog URL accepted; queued next local track");
        } else {
            ESP_LOGW(TAG, "Music catalog ended: %s",
                     result == MusicCatalog::Result::Finished ? "no more songs" : "invalid page or URL resolution failed");
        }
    }
    // Network requests are scheduled by the application, never under this lock.
    if (request_more && cloud_request_ready_) cloud_request_ready_(generation);
    if (queued && task_) xTaskNotifyGive(task_);
    return true;
}

void MusicPlayer::BeginTurn() {
    // Pause the live stream at its PCM boundary; do not discard HTTP/decoder
    // state just because the user starts another voice turn.
    {
        std::lock_guard<std::mutex> lock(request_mutex_);
        CancelCloudRequestLocked();
        if (playback_state_.PauseForTurn()) {
            if (pause_output_) pause_output_();
            ESP_LOGI(TAG, "Music control: gen=%u reason=voice_turn paused=1", (unsigned)request_generation_.load());
        } else {
            request_generation_.fetch_add(1);
            pending_urls_.clear();
            if (cancel_output_) cancel_output_();
        }
        gate_.BeginTurn();
        pending_ticket_ = gate_.Arm(false);
    }
    if (task_) xTaskNotifyGive(task_);
}

void MusicPlayer::NotifyTtsStarted() {
    std::lock_guard<std::mutex> lock(request_mutex_);
    CancelCloudRequestLocked();
    gate_.NotifyTtsStarted();
    if (playback_state_.PauseForTts()) {
        if (pause_output_) pause_output_();
        ESP_LOGI(TAG, "Music control: gen=%u reason=tts_start paused=1", (unsigned)request_generation_.load());
    }
}

void MusicPlayer::NotifyTtsFinished() {
    {
        std::lock_guard<std::mutex> lock(request_mutex_);
        gate_.NotifyTtsFinished();
        if (playback_state_.OnTtsFinished(gate_)) {
            if (resume_output_) resume_output_();
            ESP_LOGI(TAG, "Music resumed after TTS; continuing retained stream (gen=%u)",
                     (unsigned)request_generation_.load());
        }
    }
    if (task_) xTaskNotifyGive(task_);
}

void MusicPlayer::NotifyTtsAborted() {
    std::lock_guard<std::mutex> lock(request_mutex_);
    CancelCloudRequestLocked();
    gate_.NotifyTtsAborted();
    if (playback_state_.PauseForTts()) {
        if (pause_output_) pause_output_();
        ESP_LOGI(TAG, "Music control: gen=%u reason=tts_abort paused=1", (unsigned)request_generation_.load());
    }
    if (task_) xTaskNotifyGive(task_);
}

void MusicPlayer::Stop() {
    {
        std::lock_guard<std::mutex> lock(request_mutex_);
        ESP_LOGI(TAG, "Music control: gen=%u reason=local_stop", (unsigned)request_generation_.load());
        CancelCloudRequestLocked();
        request_generation_.fetch_add(1);
        playback_state_.Stop();
        pending_urls_.clear();
        pending_ticket_ = gate_.Arm(false);
        // Cancel only music PCM. TTS frames can share the output queue.
        if (cancel_output_) cancel_output_();
    }
    if (task_) xTaskNotifyGive(task_);
}

std::string MusicPlayer::PrepareCloudRequest(uint32_t generation, uint64_t now_ms) {
    std::lock_guard<std::mutex> lock(request_mutex_);
    if (!running_.load() || !auto_next_ready_ || request_generation_.load() != generation) return {};
    auto_next_ready_ = false;
    const std::string biz_id = "music-" + std::to_string(generation) + "-" + std::to_string(now_ms) +
                               "-" + std::to_string(++cloud_request_sequence_);
    std::string request = catalog_.Request(biz_id, std::time(nullptr));
    if (request.empty()) return {};
    auto_next_deadline_ms_ = now_ms + 30000;
    return request;
}

bool MusicPlayer::CanPublishAutoNext(uint32_t generation, uint64_t now_ms) {
    std::lock_guard<std::mutex> lock(request_mutex_);
    return running_.load() && request_generation_.load() == generation &&
           auto_next_deadline_ms_ != 0 && now_ms < auto_next_deadline_ms_;
}

bool MusicPlayer::ExpireAutoNext(uint64_t now_ms) {
    std::lock_guard<std::mutex> lock(request_mutex_);
    if (!auto_next_deadline_ms_ || now_ms < auto_next_deadline_ms_) return false;
    CancelCloudRequestLocked();
    return true;
}

void MusicPlayer::CancelAutoNext() {
    std::lock_guard<std::mutex> lock(request_mutex_);
    CancelCloudRequestLocked();
}

void MusicPlayer::TaskEntry(void* arg) {
    auto* self = static_cast<MusicPlayer*>(arg);
    self->TaskLoop();
    xSemaphoreGive(self->task_stopped_);
    vTaskSuspend(nullptr);
}

void MusicPlayer::TaskLoop() {
    while (running_.load()) {
        bool pending;
        {
            std::lock_guard<std::mutex> lock(request_mutex_);
            pending = !pending_urls_.empty();
        }
        // Waits inside a previous stream/TTS gate may have consumed a wakeup.
        // Pending work, not notification count, is the source of truth.
        if (!pending) ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        if (!running_.load()) break;

        std::vector<std::string> urls;
        MusicStartGate::Ticket ticket;
        uint32_t generation;
        {
            std::lock_guard<std::mutex> lock(request_mutex_);
            urls = pending_urls_;
            pending_urls_.clear();
            ticket = pending_ticket_;
            generation = request_generation_.load();
        }
        if (urls.empty()) continue;

        if (ticket.wait_for_tts || gate_.TtsActive()) {
            const TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(15000);
            while (!IsCancelled(generation) && !gate_.Expired(ticket) &&
                   ((ticket.wait_for_tts && !gate_.Ready(ticket)) || gate_.TtsActive())) {
                if ((int32_t)(deadline - xTaskGetTickCount()) <= 0) break;
                ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(100));
            }
            if (IsCancelled(generation) || gate_.Expired(ticket) ||
                (ticket.wait_for_tts && !gate_.Ready(ticket)) || gate_.TtsActive()) {
                if (!IsCancelled(generation)) {
                    ESP_LOGW(TAG, "TTS not ready; skipping music");
                }
                continue;
            }
        }
        // Even cards without preTtsFlag must not clear or overtake already
        // queued speech. Wait for TTS PCM to finish before music starts.
        if (wait_for_output_) wait_for_output_();
        ESP_LOGI(TAG, "Starting music download (free internal=%u)",
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
        uint32_t playback_generation = 0;
        {
            std::lock_guard<std::mutex> lock(request_mutex_);
            if (IsCancelled(generation)) continue;
            playback_generation = begin_playback_ ? begin_playback_() : 0;
            playback_state_.StartTrack();
        }
        bool completed = true;
        for (const auto& url : urls) {
            if (IsCancelled(generation)) break;
            if (!StreamMp3(url, generation, playback_generation)) {
                completed = false;
                if (!IsCancelled(generation)) ESP_LOGW(TAG, "Music track download/decode failed");
                break;
            }
        }
        // Decoding can finish while the final PCM frames are still queued.
        if (!IsCancelled(generation) && wait_for_output_) wait_for_output_();
        bool notify_finished = false;
        {
            std::lock_guard<std::mutex> lock(request_mutex_);
            if (!IsCancelled(generation)) {
                auto_next_ready_ = auto_next_enabled_ && music_playlist_ && completed &&
                                   !playback_state_.paused() && !gate_.TtsActive() && catalog_.HasNext();
                notify_finished = auto_next_ready_;
                playback_state_.Stop();
            }
        }
        ESP_LOGI(TAG, "Music playlist end: gen=%u tracks=%u outcome=%s output_wait_done=%d auto_next=%d",
                 (unsigned)generation, (unsigned)urls.size(),
                 IsCancelled(generation) ? "cancelled" : (completed ? "completed" : "failed"),
                 !IsCancelled(generation) && bool(wait_for_output_), notify_finished);
        // Never call application/network code with the player mutex held.
        if (notify_finished && cloud_request_ready_) cloud_request_ready_(generation);
    }
}

bool MusicPlayer::IsCancelled(uint32_t generation) const {
    return !running_.load() || request_generation_.load() != generation;
}

bool MusicPlayer::StreamMp3(const std::string& url, uint32_t request_generation,
                            uint32_t playback_generation) {
    bool success = false;
    bool received_data = false;
    void* decoder = nullptr;
    esp_ae_rate_cvt_handle_t resampler = nullptr;
    uint32_t resampler_rate = 0;
    std::vector<uint8_t> encoded;
    encoded.reserve(MUSIC_HTTP_READ_SIZE * 2);
    uint8_t read_buffer[MUSIC_HTTP_READ_SIZE];
    MusicHttpResume resume;
    unsigned reconnects = 0;
    int last_error = 0;
    int64_t last_read_us = esp_timer_get_time();
    const int64_t started_us = last_read_us;
    int64_t progress_us = started_us;
    uint64_t queued_samples = 0;
    const uint32_t resource = MusicResourceId(url);
    const char* reason = "http_init";
    ESP_LOGI(TAG, "Music track start: gen=%u resource=%08x sample_rate=%d",
             (unsigned)request_generation, (unsigned)resource, codec_->output_sample_rate());
    enum class Attempt { Complete, Reconnect, Failed, Cancelled };

    // Backoff is interruptible. Do not reconnect while the retained music is
    // paused; its decoder and PCM survive until the user resumes or cancels.
    auto wait_retry = [&](unsigned delay_ms) {
        uint64_t waited_ms = 0;
        while (!IsCancelled(request_generation)) {
            bool paused;
            {
                std::lock_guard<std::mutex> lock(request_mutex_);
                paused = playback_state_.paused();
            }
            if (!paused && waited_ms >= delay_ms) return true;
            const int64_t begin = esp_timer_get_time();
            // Leave notifications for TaskLoop: a replacement playlist can
            // cancel this stream while posting its own worker wakeup.
            vTaskDelay(pdMS_TO_TICKS(100));
            if (!paused) waited_ms += (esp_timer_get_time() - begin) / 1000;
        }
        return false;
    };

    // Reconnect only the HTTP handle. Never reset decoder/resampler, retained
    // encoded bytes, playback generation, or already queued PCM here.
    while (!IsCancelled(request_generation)) {
        MusicHttpHeaders headers;
        esp_http_client_config_t http_config = {};
        http_config.url = url.c_str();
        http_config.timeout_ms = 10000;
        http_config.buffer_size = MUSIC_HTTP_READ_SIZE;
        http_config.crt_bundle_attach = esp_crt_bundle_attach;
        http_config.user_data = &headers;
        http_config.event_handler = [](esp_http_client_event_t* event) -> esp_err_t {
            if (event->event_id == HTTP_EVENT_ON_HEADER) {
                static_cast<MusicHttpHeaders*>(event->user_data)->Record(event->header_key, event->header_value);
            }
            return ESP_OK;
        };
        auto http = esp_http_client_init(&http_config);
        if (!http) {
            reason = "http_init";
            ESP_LOGE(TAG, "Music HTTP init failed (free internal=%u)",
                     (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
            break;
        }

        const Attempt attempt = [&]() -> Attempt {
            reason = "http_header_setup";
            if (esp_http_client_set_header(http, "Accept-Encoding", "identity") != ESP_OK) return Attempt::Failed;
            if (resume.offset) {
                const std::string range = "bytes=" + std::to_string(resume.offset) + "-";
                if (esp_http_client_set_header(http, "Range", range.c_str()) != ESP_OK ||
                    esp_http_client_set_header(http, "If-Range", resume.etag.c_str()) != ESP_OK) return Attempt::Failed;
            }
            const auto open_result = esp_http_client_open(http, 0);
            if (open_result != ESP_OK) {
                reason = "http_open";
                last_error = esp_http_client_get_errno(http);
                ESP_LOGW(TAG, "Music HTTP open failed: %s errno=%d offset=%llu",
                         esp_err_to_name(open_result), last_error, (unsigned long long)resume.offset);
                return MusicHttpResume::TransientError(last_error) ? Attempt::Reconnect : Attempt::Failed;
            }
            if (esp_http_client_fetch_headers(http) < 0) {
                reason = "http_headers";
                last_error = esp_http_client_get_errno(http);
                ESP_LOGW(TAG, "Music HTTP headers failed: errno=%d offset=%llu",
                         last_error, (unsigned long long)resume.offset);
                return MusicHttpResume::TransientError(last_error) ? Attempt::Reconnect : Attempt::Failed;
            }
            if (IsCancelled(request_generation)) return Attempt::Cancelled;
            const int status = esp_http_client_get_status_code(http);
            if (!resume.AcceptResponse(status, esp_http_client_get_content_length(http), headers)) {
                reason = "http_response";
                ESP_LOGW(TAG, "Music HTTP response rejected: status=%d offset=%llu (range/resource mismatch)",
                         status, (unsigned long long)resume.offset);
                return Attempt::Failed;
            }
            last_error = 0;  // This connection recovered; do not blame a later decoder fault on it.
            if (!decoder) {
                reason = "decoder_init";
                if (!esp_audio_dec_get_ops(ESP_AUDIO_TYPE_MP3) &&
                    esp_mp3_dec_register() != ESP_AUDIO_ERR_OK) return Attempt::Failed;
                esp_audio_simple_dec_cfg_t cfg = {};
                cfg.dec_type = ESP_AUDIO_SIMPLE_DEC_TYPE_MP3;
                cfg.use_frame_dec = false;
                const auto result = esp_audio_simple_dec_open(&cfg, &decoder);
                if (result != ESP_AUDIO_ERR_OK || !decoder) {
                    ESP_LOGE(TAG, "Failed to open MP3 stream decoder: %d", result);
                    return Attempt::Failed;
                }
            }
            ESP_LOGI(TAG, "Music HTTP %d, MP3 stream decoder ready (offset=%llu total=%lld reconnects=%u)",
                     status, (unsigned long long)resume.offset, (long long)resume.total, reconnects);

            while (!IsCancelled(request_generation)) {
                errno = 0;
                const int received = esp_http_client_read(http, (char*)read_buffer, sizeof(read_buffer));
                if (IsCancelled(request_generation)) return Attempt::Cancelled;
                if (received <= 0) {
                    if (received == 0 && esp_http_client_is_complete_data_received(http) && resume.AtEnd()) {
                        // Only real resource EOF may flush the final lookahead
                        // into the decoder. Transport loss is NOT decoder EOS.
                        if (!received_data) { reason = "empty_resource"; return Attempt::Failed; }
                        reason = "decode_or_output";
                        if (!DecodeAvailable(decoder, encoded, request_generation,
                            playback_generation, &resampler, &resampler_rate, true, &queued_samples)) return Attempt::Failed;
                        reason = "resource_eof";
                        return Attempt::Complete;
                    }
                    last_error = esp_http_client_get_errno(http);
                    reason = "http_read";
                    if (!last_error) last_error = errno;
                    ESP_LOGW(TAG, "Music HTTP read interrupted: rc=%d errno=%d offset=%llu total=%lld idle_ms=%lld",
                             received, last_error, (unsigned long long)resume.offset, (long long)resume.total,
                             (long long)((esp_timer_get_time() - last_read_us) / 1000));
                    return received == 0 || received == -ESP_ERR_HTTP_EAGAIN ||
                           MusicHttpResume::TransientError(last_error) ? Attempt::Reconnect : Attempt::Failed;
                }
                if (!resume.CanAcceptBytes(received)) {
                    reason = "body_length";
                    ESP_LOGW(TAG, "Music HTTP body exceeds declared resource length");
                    return Attempt::Failed;
                }
                // Keep the existing one-chunk lookahead across reconnections.
                reason = "decode_or_output";
                if (!DecodeAvailable(decoder, encoded, request_generation, playback_generation,
                                     &resampler, &resampler_rate, false, &queued_samples)) return Attempt::Failed;
                if (encoded.size() + size_t(received) > MUSIC_ENCODED_BUFFER_LIMIT) {
                    reason = "encoded_buffer_limit";
                    ESP_LOGW(TAG, "MP3 decoder retained too much incomplete input");
                    return Attempt::Failed;
                }
                encoded.insert(encoded.end(), read_buffer, read_buffer + received);
                resume.offset += received;
                received_data = true;
                last_read_us = esp_timer_get_time();
                if (last_read_us - progress_us >= 15000000) {
                    ESP_LOGD(TAG, "Music progress: gen=%u resource=%08x bytes=%llu total=%lld "
                             "pcm_queued_ms=%llu wall_ms=%lld reconnects=%u",
                             (unsigned)request_generation, (unsigned)resource, (unsigned long long)resume.offset,
                             (long long)resume.total,
                             (unsigned long long)(queued_samples * 1000 / codec_->output_sample_rate()),
                             (long long)((last_read_us - started_us) / 1000), reconnects);
                    progress_us = last_read_us;
                }
            }
            return Attempt::Cancelled;
        }();

        esp_http_client_close(http);
        esp_http_client_cleanup(http);
        if (attempt == Attempt::Complete) { success = true; break; }
        if (attempt != Attempt::Reconnect || IsCancelled(request_generation)) break;
        if (reconnects >= MUSIC_HTTP_MAX_RECONNECTS || !resume.CanReconnect()) {
            reason = reconnects >= MUSIC_HTTP_MAX_RECONNECTS ? "reconnect_limit" : "no_safe_validator";
            ESP_LOGW(TAG, "Music HTTP recovery stopped: offset=%llu reconnects=%u (limit or no safe validator)",
                     (unsigned long long)resume.offset, reconnects);
            break;
        }
        const unsigned delay_ms = 1000U << reconnects++;
        ESP_LOGW(TAG, "Music HTTP reconnect %u/%u: offset=%llu errno=%d backoff_ms=%u",
                 reconnects, MUSIC_HTTP_MAX_RECONNECTS, (unsigned long long)resume.offset, last_error, delay_ms);
        if (!wait_retry(delay_ms)) break;
    }

    const bool cancelled = IsCancelled(request_generation);
    ESP_LOGI(TAG, "Music track end: gen=%u resource=%08x outcome=%s reason=%s bytes=%llu total=%lld "
             "queued_samples=%llu sample_rate=%d pcm_queued_ms=%llu wall_ms=%lld reconnects=%u errno=%d "
             "encoded_pending=%u free_internal=%u",
             (unsigned)request_generation, (unsigned)resource,
             cancelled ? "cancelled" : (success ? "completed" : "failed"), cancelled ? "cancelled" : reason,
             (unsigned long long)resume.offset, (long long)resume.total, (unsigned long long)queued_samples,
             codec_->output_sample_rate(),
             (unsigned long long)(queued_samples * 1000 / codec_->output_sample_rate()),
             (long long)((esp_timer_get_time() - started_us) / 1000), reconnects,
             success ? 0 : last_error, (unsigned)encoded.size(),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    if (resampler) esp_ae_rate_cvt_close(resampler);
    if (decoder) esp_audio_simple_dec_close(decoder);
    return success && !IsCancelled(request_generation);
}

bool MusicPlayer::DecodeAvailable(void* decoder, std::vector<uint8_t>& encoded,
                                  uint32_t request_generation, uint32_t playback_generation,
                                  esp_ae_rate_cvt_handle_t* resampler, uint32_t* resampler_rate,
                                  bool eos, uint64_t* queued_samples) {
    // A stereo MPEG-1 Layer III frame needs at most 1152 * 2 * 2 PCM bytes.
    // Reuse this buffer throughout the HTTP chunk instead of reallocating per frame.
    std::vector<uint8_t> decoded(4608);
    while (!encoded.empty() && !IsCancelled(request_generation)) {
        esp_audio_simple_dec_raw_t raw = {
            .buffer = encoded.data(),
            .len = (uint32_t)encoded.size(),
            .eos = eos,
            .consumed = 0,
            .frame_recover = ESP_AUDIO_SIMPLE_DEC_RECOVERY_NONE,
        };
        esp_audio_simple_dec_out_t frame = {
            .buffer = decoded.data(),
            .len = (uint32_t)decoded.size(),
            .needed_size = 0,
            .decoded_size = 0,
        };
        esp_audio_err_t result = esp_audio_simple_dec_process(decoder, &raw, &frame);
        if (result == ESP_AUDIO_ERR_BUFF_NOT_ENOUGH) {
            if (frame.needed_size <= decoded.size() ||
                frame.needed_size > MUSIC_DECODED_BUFFER_LIMIT) return false;
            decoded.resize(frame.needed_size);
            raw.consumed = 0;
            frame.buffer = decoded.data();
            frame.len = (uint32_t)decoded.size();
            frame.decoded_size = 0;
            result = esp_audio_simple_dec_process(decoder, &raw, &frame);
        }

        if (raw.consumed > encoded.size()) return false;
        if (raw.consumed > 0) {
            encoded.erase(encoded.begin(), encoded.begin() + raw.consumed);
        }
        if (result == ESP_AUDIO_ERR_DATA_LACK || result == ESP_AUDIO_ERR_CONTINUE) {
            return !eos || encoded.empty();
        }
        if (result != ESP_AUDIO_ERR_OK) {
            // The parser owns frame synchronization. Do not discard arbitrary
            // bytes on decoder errors, which could silently skip valid frames.
            ESP_LOGE(TAG, "MP3 stream decode failed: %d", result);
            return false;
        }
        if (frame.decoded_size == 0) {
            if (raw.consumed == 0) return !eos;
            continue;
        }
        esp_audio_simple_dec_info_t info = {};
        if (esp_audio_simple_dec_get_info(decoder, &info) != ESP_AUDIO_ERR_OK ||
            info.channel == 0 || info.channel > 2 || info.sample_rate == 0 ||
            info.bits_per_sample != 16 || frame.decoded_size > decoded.size() ||
            frame.decoded_size % (sizeof(int16_t) * info.channel) != 0) return false;

        const size_t sample_count = frame.decoded_size / sizeof(int16_t);
        const size_t channel_count = info.channel;
        if (*queued_samples == 0) {
            ESP_LOGI(TAG, "Music PCM format: gen=%u input_rate=%u channels=%u bits=%u output_rate=%d",
                     (unsigned)request_generation, (unsigned)info.sample_rate, (unsigned)info.channel,
                     (unsigned)info.bits_per_sample, codec_->output_sample_rate());
        }
        std::vector<int16_t> mono(sample_count / channel_count);
        const auto* samples = reinterpret_cast<const int16_t*>(decoded.data());
        for (size_t i = 0, out = 0; i + channel_count <= sample_count; i += channel_count, ++out) {
            int32_t sum = 0;
            for (size_t channel = 0; channel < channel_count; ++channel) sum += samples[i + channel];
            mono[out] = (int16_t)(sum / (int32_t)channel_count);
        }

        if (info.sample_rate != (uint32_t)codec_->output_sample_rate()) {
            if (!*resampler || *resampler_rate != info.sample_rate) {
                if (*resampler) esp_ae_rate_cvt_close(*resampler);
                *resampler = nullptr;
                esp_ae_rate_cvt_cfg_t cfg = {
                    .src_rate = info.sample_rate,
                    .dest_rate = (uint32_t)codec_->output_sample_rate(),
                    .channel = 1,
                    .bits_per_sample = ESP_AUDIO_BIT16,
                    .complexity = 2,
                    .perf_type = ESP_AE_RATE_CVT_PERF_TYPE_SPEED,
                };
                if (esp_ae_rate_cvt_open(&cfg, resampler) != ESP_AE_ERR_OK || !*resampler) return false;
                *resampler_rate = info.sample_rate;
            }
            uint32_t max_samples = 0;
            if (esp_ae_rate_cvt_get_max_out_sample_num(*resampler, mono.size(), &max_samples) != ESP_AE_ERR_OK) return false;
            std::vector<int16_t> converted(max_samples);
            uint32_t actual_samples = max_samples;
            if (esp_ae_rate_cvt_process(*resampler, (esp_ae_sample_t)mono.data(), mono.size(),
                                        (esp_ae_sample_t)converted.data(), &actual_samples) != ESP_AE_ERR_OK ||
                actual_samples > converted.size()) return false;
            converted.resize(actual_samples);
            mono = std::move(converted);
        }
        if (!mono.empty() && pcm_sink_) {
            const size_t output_samples = mono.size();
            if (!pcm_sink_(std::move(mono), playback_generation)) return false;
            *queued_samples += output_samples;
        }
    }
    return !IsCancelled(request_generation);
}
