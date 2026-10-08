#ifndef MUSIC_PLAYER_H
#define MUSIC_PLAYER_H

#include <atomic>
#include <functional>
#include <mutex>
#include <string>
#include <vector>

#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>
#include <esp_ae_rate_cvt.h>

#include "audio_codec.h"
#include "music_playback_state.h"
#include "music_start_gate.h"

struct cJSON;

class MusicPlayer {
public:
    using BeginPlayback = std::function<uint32_t()>;
    using PcmSink = std::function<bool(std::vector<int16_t>&&, uint32_t)>;
    using WaitForOutput = std::function<void()>;
    using CancelOutput = std::function<void()>;
    using PauseOutput = std::function<void()>;
    using ResumeOutput = std::function<void()>;
    using PlaybackFinished = std::function<void(uint32_t)>;

    MusicPlayer(AudioCodec* codec, BeginPlayback begin_playback, PcmSink pcm_sink,
                WaitForOutput wait_for_output, CancelOutput cancel_output,
                PauseOutput pause_output, ResumeOutput resume_output,
                PlaybackFinished playback_finished = {}, bool auto_next_enabled = false);
    ~MusicPlayer();

    bool Start();
    void Shutdown();
    bool HandleSkillCard(const cJSON* skill_card);
    void BeginTurn();
    void NotifyTtsStarted();
    void NotifyTtsFinished();
    void NotifyTtsAborted();
    void Stop();
    bool ConsumeAutoNext(uint32_t generation, uint64_t now_ms);
    bool ExpireAutoNext(uint64_t now_ms);
    void CancelAutoNext();

private:
    static void TaskEntry(void* arg);
    void TaskLoop();
    bool StreamMp3(const std::string& url, uint32_t request_generation,
                   uint32_t playback_generation);
    bool DecodeAvailable(void* decoder, std::vector<uint8_t>& encoded,
                         uint32_t request_generation, uint32_t playback_generation,
                         esp_ae_rate_cvt_handle_t* resampler, uint32_t* resampler_rate,
                         bool eos);
    bool IsCancelled(uint32_t generation) const;

    AudioCodec* codec_;
    BeginPlayback begin_playback_;
    PcmSink pcm_sink_;
    WaitForOutput wait_for_output_;
    CancelOutput cancel_output_;
    PauseOutput pause_output_;
    ResumeOutput resume_output_;
    PlaybackFinished playback_finished_;
    const bool auto_next_enabled_;
    bool music_playlist_ = false;
    bool auto_next_ready_ = false;
    uint64_t auto_next_deadline_ms_ = 0;  // Guarded by request_mutex_; no automatic retries.

    mutable std::mutex request_mutex_;
    std::vector<std::string> pending_urls_;
    MusicStartGate gate_;
    MusicPlaybackState playback_state_;  // Guarded by request_mutex_.
    MusicStartGate::Ticket pending_ticket_{false, 0};
    std::atomic<uint32_t> request_generation_{0};
    std::atomic<bool> running_{false};
    TaskHandle_t task_ = nullptr;
    StaticSemaphore_t task_stopped_storage_;
    SemaphoreHandle_t task_stopped_ = nullptr;
};

#endif
