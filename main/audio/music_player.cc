#include "music_player.h"

#include <algorithm>
#include <cstring>

#include <esp_crt_bundle.h>
#include <esp_http_client.h>
#include <esp_log.h>
#include <esp_heap_caps.h>
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

MusicPlayer::MusicPlayer(AudioCodec* codec, BeginPlayback begin_playback, PcmSink pcm_sink,
                         WaitForOutput wait_for_output, CancelOutput cancel_output,
                         PauseOutput pause_output, ResumeOutput resume_output,
                         PlaybackFinished playback_finished, bool auto_next_enabled)
    : codec_(codec), begin_playback_(std::move(begin_playback)), pcm_sink_(std::move(pcm_sink)),
      wait_for_output_(std::move(wait_for_output)), cancel_output_(std::move(cancel_output)),
      pause_output_(std::move(pause_output)), resume_output_(std::move(resume_output)),
      playback_finished_(std::move(playback_finished)), auto_next_enabled_(auto_next_enabled) {
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

void MusicPlayer::Shutdown() {
    if (!running_.exchange(false)) return;
    request_generation_.fetch_add(1);
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
    if (strcmp(action, "stop") == 0) {
        // Tuya PlayControl uses "stop" for pause. Keep the active stream and
        // queued PCM so a later resume card (without URLs) can continue it.
        std::lock_guard<std::mutex> lock(request_mutex_);
        auto_next_ready_ = false;
        auto_next_deadline_ms_ = 0;
        if (playback_state_.PauseForTurn()) {
            if (pause_output_) pause_output_();
            ESP_LOGI(TAG, "Music paused; stream retained for resume");
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

    const cJSON* data = container ? cJSON_GetObjectItem(container, "data") : nullptr;
    const cJSON* pre_tts = cJSON_IsObject(data) ? cJSON_GetObjectItem(data, "preTtsFlag") : nullptr;
    const bool wait_for_tts = cJSON_IsTrue(pre_tts);
    if (strcmp(action, "resume") == 0) {
        std::lock_guard<std::mutex> lock(request_mutex_);
        if (!playback_state_.RequestResume(gate_, wait_for_tts)) {
            ESP_LOGW(TAG, "Cannot resume: no interrupted music stream");
            return false;
        }
        if (playback_state_.OnTtsFinished(gate_) && resume_output_) resume_output_();
        ESP_LOGI(TAG, "Music resume %s", playback_state_.paused() ? "awaiting TTS" : "ready");
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
        playback_state_.Stop();
        if (cancel_output_) cancel_output_();
        pending_urls_ = std::move(urls);
        music_playlist_ = strcmp(code, "music") == 0 ||
                          (strcmp(code, "PlayControl") == 0 && music_playlist_);
        auto_next_ready_ = false;
        auto_next_deadline_ms_ = 0;
        pending_ticket_ = gate_.Arm(wait_for_tts);
        request_generation_.fetch_add(1);
    }
    xTaskNotifyGive(task_);
    ESP_LOGI(TAG, "Queued music playlist (%u tracks)", (unsigned)track_count);
    return true;
}

void MusicPlayer::BeginTurn() {
    // Pause the live stream at its PCM boundary; do not discard HTTP/decoder
    // state just because the user starts another voice turn.
    {
        std::lock_guard<std::mutex> lock(request_mutex_);
        auto_next_ready_ = false;
        auto_next_deadline_ms_ = 0;
        if (playback_state_.PauseForTurn()) {
            if (pause_output_) pause_output_();
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
    auto_next_ready_ = false;
    auto_next_deadline_ms_ = 0;
    gate_.NotifyTtsStarted();
    if (playback_state_.PauseForTts() && pause_output_) pause_output_();
}

void MusicPlayer::NotifyTtsFinished() {
    {
        std::lock_guard<std::mutex> lock(request_mutex_);
        gate_.NotifyTtsFinished();
        if (playback_state_.OnTtsFinished(gate_)) {
            if (resume_output_) resume_output_();
            ESP_LOGI(TAG, "Music resumed after TTS; continuing retained stream");
        }
    }
    if (task_) xTaskNotifyGive(task_);
}

void MusicPlayer::NotifyTtsAborted() {
    std::lock_guard<std::mutex> lock(request_mutex_);
    auto_next_ready_ = false;
    auto_next_deadline_ms_ = 0;
    gate_.NotifyTtsAborted();
    if (playback_state_.PauseForTts() && pause_output_) pause_output_();
    if (task_) xTaskNotifyGive(task_);
}

void MusicPlayer::Stop() {
    {
        std::lock_guard<std::mutex> lock(request_mutex_);
        auto_next_ready_ = false;
        auto_next_deadline_ms_ = 0;
        request_generation_.fetch_add(1);
        playback_state_.Stop();
        pending_urls_.clear();
        pending_ticket_ = gate_.Arm(false);
        // Cancel only music PCM. TTS frames can share the output queue.
        if (cancel_output_) cancel_output_();
    }
    if (task_) xTaskNotifyGive(task_);
}

bool MusicPlayer::ConsumeAutoNext(uint32_t generation, uint64_t now_ms) {
    std::lock_guard<std::mutex> lock(request_mutex_);
    if (!running_.load() || !auto_next_ready_ || request_generation_.load() != generation) return false;
    auto_next_ready_ = false;
    auto_next_deadline_ms_ = now_ms + 30000;
    return true;
}

bool MusicPlayer::ExpireAutoNext(uint64_t now_ms) {
    std::lock_guard<std::mutex> lock(request_mutex_);
    if (!auto_next_deadline_ms_ || now_ms < auto_next_deadline_ms_) return false;
    auto_next_deadline_ms_ = 0;
    return true;
}

void MusicPlayer::CancelAutoNext() {
    std::lock_guard<std::mutex> lock(request_mutex_);
    auto_next_ready_ = false;
    auto_next_deadline_ms_ = 0;
}

void MusicPlayer::TaskEntry(void* arg) {
    auto* self = static_cast<MusicPlayer*>(arg);
    self->TaskLoop();
    xSemaphoreGive(self->task_stopped_);
    vTaskSuspend(nullptr);
}

void MusicPlayer::TaskLoop() {
    while (running_.load()) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
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
                                   !playback_state_.paused() && !gate_.TtsActive();
                notify_finished = auto_next_ready_;
                playback_state_.Stop();
            }
        }
        // Never call application/network code with the player mutex held.
        if (notify_finished && playback_finished_) playback_finished_(generation);
    }
}

bool MusicPlayer::IsCancelled(uint32_t generation) const {
    return !running_.load() || request_generation_.load() != generation;
}

bool MusicPlayer::StreamMp3(const std::string& url, uint32_t request_generation,
                            uint32_t playback_generation) {
    esp_http_client_config_t http_config = {};
    http_config.url = url.c_str();
    http_config.timeout_ms = 10000;
    http_config.buffer_size = MUSIC_HTTP_READ_SIZE;
    http_config.crt_bundle_attach = esp_crt_bundle_attach;
    esp_http_client_handle_t http = esp_http_client_init(&http_config);
    if (!http) return false;

    bool success = false;
    bool received_data = false;
    void* decoder = nullptr;
    esp_ae_rate_cvt_handle_t resampler = nullptr;
    uint32_t resampler_rate = 0;
    std::vector<uint8_t> encoded;
    encoded.reserve(MUSIC_HTTP_READ_SIZE * 2);
    uint8_t read_buffer[MUSIC_HTTP_READ_SIZE];

    {
        esp_err_t open_result = esp_http_client_open(http, 0);
        if (open_result != ESP_OK) {
            ESP_LOGE(TAG, "Music HTTPS open failed: %s (free internal=%u)",
                     esp_err_to_name(open_result),
                     (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
            goto cleanup;
        }
    }
    esp_http_client_fetch_headers(http);
    if (esp_http_client_get_status_code(http) < 200 || esp_http_client_get_status_code(http) >= 300) {
        ESP_LOGW(TAG, "Music URL returned HTTP %d", esp_http_client_get_status_code(http));
        goto cleanup;
    }
    {
        // Simple Decoder uses the common frame-decoder registry. Register only
        // MP3, without changing or allocating other audio decoder types.
        if (!esp_audio_dec_get_ops(ESP_AUDIO_TYPE_MP3) &&
            esp_mp3_dec_register() != ESP_AUDIO_ERR_OK) goto cleanup;
        esp_audio_simple_dec_cfg_t cfg = {};
        cfg.dec_type = ESP_AUDIO_SIMPLE_DEC_TYPE_MP3;
        cfg.use_frame_dec = false;  // HTTP chunks are not MP3 frame boundaries.
        const auto result = esp_audio_simple_dec_open(&cfg, &decoder);
        if (result != ESP_AUDIO_ERR_OK || !decoder) {
            ESP_LOGE(TAG, "Failed to open MP3 stream decoder: %d", result);
            goto cleanup;
        }
    }
    ESP_LOGI(TAG, "Music HTTP %d, MP3 stream decoder ready", esp_http_client_get_status_code(http));

    while (!IsCancelled(request_generation)) {
        int received = esp_http_client_read(http, (char*)read_buffer, sizeof(read_buffer));
        if (received < 0) break;
        if (received == 0) {
            if (!esp_http_client_is_complete_data_received(http)) {
                ESP_LOGW(TAG, "Music HTTP stream ended before the body was complete");
                break;
            }
            // Retain one HTTP chunk as lookahead, so the parser receives EOS
            // with real final data rather than inferring it from a short read.
            success = received_data && DecodeAvailable(decoder, encoded, request_generation, playback_generation,
                                      &resampler, &resampler_rate, true);
            break;
        }
        received_data = true;
        if (!DecodeAvailable(decoder, encoded, request_generation, playback_generation,
                             &resampler, &resampler_rate, false)) break;
        if (encoded.size() + (size_t)received > MUSIC_ENCODED_BUFFER_LIMIT) {
            ESP_LOGW(TAG, "MP3 decoder retained too much incomplete input");
            break;
        }
        encoded.insert(encoded.end(), read_buffer, read_buffer + received);
    }

cleanup:
    if (resampler) esp_ae_rate_cvt_close(resampler);
    if (decoder) esp_audio_simple_dec_close(decoder);
    esp_http_client_close(http);
    esp_http_client_cleanup(http);
    return success && !IsCancelled(request_generation);
}

bool MusicPlayer::DecodeAvailable(void* decoder, std::vector<uint8_t>& encoded,
                                  uint32_t request_generation, uint32_t playback_generation,
                                  esp_ae_rate_cvt_handle_t* resampler, uint32_t* resampler_rate,
                                  bool eos) {
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
        if (!mono.empty() && pcm_sink_ && !pcm_sink_(std::move(mono), playback_generation)) return false;
    }
    return !IsCancelled(request_generation);
}
