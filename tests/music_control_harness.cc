#include <algorithm>
#include <atomic>
#include <cassert>
#include <cstring>
#include <functional>
#include <mutex>
#include <string>
#include <vector>

#include <cJSON.h>
#include "music_playback_state.h"

#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define MUSIC_MAX_URLS 16
#define MUSIC_MAX_URL_LENGTH 2048
void xTaskNotifyGive(void*) {}

// Run the production card handler and control methods without HTTP/FreeRTOS.
struct MusicPlayer {
    MusicPlaybackState playback_state_;
    MusicStartGate gate_;
    std::mutex request_mutex_;
    std::vector<std::string> pending_urls_;
    MusicStartGate::Ticket pending_ticket_{false, 0};
    std::atomic<uint32_t> request_generation_{7};
    void* task_ = nullptr;
    std::function<void()> cancel_output_, pause_output_, resume_output_;
    bool Start() { return true; }
    bool HandleSkillCard(const cJSON*);
    void BeginTurn();
    void NotifyTtsStarted();
    void NotifyTtsFinished();
    void NotifyTtsAborted();
    void Stop();
};

// MUSIC_CONTROL_METHODS

int main() {
    // The real log sends stop for "暂停", then resume without any URL.
    cJSON* stop = cJSON_Parse(R"({"general":{"template":{"name":"audio","version":"1.0"},"data":{"preTtsFlag":true},"action":"stop"},"code":"PlayControl","custom":{"data":{}}})");
    cJSON* resume = cJSON_Parse(R"({"general":{"template":{"name":"audio","version":"1.0"},"data":{"preTtsFlag":true},"action":"resume"},"code":"PlayControl","custom":{"data":{}}})");
    assert(stop && resume);
    MusicPlayer player;
    int cancelled = 0, paused = 0, resumed = 0;
    player.cancel_output_ = [&] { ++cancelled; };
    player.pause_output_ = [&] { ++paused; };
    player.resume_output_ = [&] { ++resumed; };
    player.playback_state_.StartTrack();
    player.BeginTurn();
    assert(player.HandleSkillCard(stop));
    assert(player.playback_state_.active());
    assert(player.playback_state_.paused());
    assert(player.request_generation_ == 7 && cancelled == 0 && paused > 0);
    player.NotifyTtsStarted();
    player.NotifyTtsFinished();
    assert(player.playback_state_.paused()); // Pause acknowledgement cannot resume.
    player.BeginTurn();
    assert(player.HandleSkillCard(resume));
    assert(resumed == 0);
    player.NotifyTtsStarted();
    assert(resumed == 0);
    player.NotifyTtsFinished();
    assert(resumed == 1 && !player.playback_state_.paused());
    assert(player.request_generation_ == 7 && cancelled == 0);
    // Internal cancellation still invalidates the stream when actually needed.
    player.Stop();
    assert(!player.playback_state_.active() && cancelled == 1);
    assert(!player.HandleSkillCard(resume));
    cJSON_Delete(stop);
    cJSON_Delete(resume);
}
