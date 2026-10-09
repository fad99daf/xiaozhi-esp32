#include <algorithm>
#include <atomic>
#include <cassert>
#include <cstring>
#include <functional>
#include <mutex>
#include <string>
#include <vector>
#include <ctime>
#include <cstdio>
#include <cstdarg>

#include <cJSON.h>
#include "music_playback_state.h"
#include "music_catalog.h"
#include "music_diagnostics.h"

static std::string logs;
void TestLog(const char*, const char* format, ...) {
    char line[1024];
    va_list args;
    va_start(args, format);
    vsnprintf(line, sizeof(line), format, args);
    va_end(args);
    logs += line;
    logs += '\n';
}
#define TAG "MusicPlayer"
#define ESP_LOGI(...) TestLog(__VA_ARGS__)
#define ESP_LOGD(...) TestLog(__VA_ARGS__)
#define ESP_LOGW(...) TestLog(__VA_ARGS__)
#define MUSIC_MAX_URLS 16
#define MUSIC_MAX_URL_LENGTH 2048
void xTaskNotifyGive(void*) {}
void xSemaphoreTake(void*, unsigned) {}
void vTaskDeleteWithCaps(void*) {}
#define portMAX_DELAY 0
static int64_t test_clock_us = 0;
int64_t esp_timer_get_time() { return test_clock_us; }

// Run the production card handler and control methods without HTTP/FreeRTOS.
struct MusicPlayer {
    MusicPlaybackState playback_state_;
    MusicStartGate gate_;
    std::mutex request_mutex_;
    std::vector<std::string> pending_urls_;
    MusicStartGate::Ticket pending_ticket_{false, 0};
    std::atomic<uint32_t> request_generation_{7};
    bool music_playlist_ = true;
    bool auto_next_ready_ = false;
    uint64_t auto_next_deadline_ms_ = 0;
    MusicCatalog catalog_;
    bool auto_next_enabled_ = true;
    uint32_t cloud_request_sequence_ = 0;
    std::function<void(uint32_t)> cloud_request_ready_;
    std::atomic<bool> running_{true};
    void* task_ = nullptr;
    void* task_stopped_ = nullptr;
    std::function<void()> cancel_output_, pause_output_, resume_output_;
    bool Start() { return true; }
    void Shutdown();
    bool HandleSkillCard(const cJSON*);
    void BeginTurn();
    void NotifyTtsStarted();
    void NotifyTtsFinished();
    void NotifyTtsAborted();
    void Stop();
    bool HandleCloudCard(const char*, const cJSON*);
    void CancelCloudRequestLocked();
    std::string PrepareCloudRequest(uint32_t, uint64_t);
};

// MUSIC_CONTROL_METHODS

int main() {
    cJSON* metadata = cJSON_Parse(R"({"audioId":"song-1","name":"test\ntrack","channelCode":"netease","durationMs":180000,"duration":30000,"url":"https://example.test/music?sign=secret","ttlSeconds":3600,"success":true})");
    LogMusicMetadata("refresh_play_url", metadata);
    assert(logs.find("audioId=song-1 name=test track channel=netease") != std::string::npos);
    assert(logs.find("durationMs=180000 duration_raw=30000") != std::string::npos);
    assert(logs.find("ttlSeconds=3600") != std::string::npos);
    assert(logs.find("secret") == std::string::npos);
    assert(logs.find("example.test") == std::string::npos);
    assert(MusicLogText(std::string(1000, 'x').c_str()).size() == 96);
    assert(MusicLogText("bad\033text\r\n") == "bad text  ");
    assert(MusicResourceId("https://a?x=1") != MusicResourceId("https://a?x=2"));
    cJSON_Delete(metadata);
    logs.clear();
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
    assert(logs.find("reason=voice_turn paused=1") != std::string::npos);
    assert(logs.find("reason=local_stop") != std::string::npos);
    assert(!player.HandleSkillCard(resume));
    cJSON_Delete(stop);
    cJSON_Delete(resume);

    player.catalog_.Reset();
    player.catalog_.Request("page", 1000);
    player.auto_next_deadline_ms_ = 30000;
    int requested = 0;
    player.cloud_request_ready_ = [&](uint32_t) { ++requested; };
    cJSON* page = cJSON_Parse(R"({"code":"PlayControl","general":{"action":"music_list","data":{"preTtsFlag":false,"page":{"offset":0,"limit":8,"hasMore":false},"items":[{"audioId":"a","channelCode":"demo"}]}}})");
    assert(player.HandleSkillCard(page));
    assert(requested == 1 && player.pending_urls_.empty()); // Metadata is not audio.
    const uint32_t generation = player.request_generation_;
    assert(player.PrepareCloudRequest(generation - 1, 1000).empty());
    const std::string request = player.PrepareCloudRequest(generation, 1000);
    cJSON* parsed = cJSON_Parse(request.c_str());
    assert(parsed);
    const cJSON* params = cJSON_GetObjectItem(cJSON_GetObjectItem(parsed, "data"), "data");
    assert(strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(params, "action")), "refresh_play_url") == 0);
    cJSON_Delete(parsed);
    assert(player.PrepareCloudRequest(generation, 1001).empty());
    assert(!player.HandleSkillCard(page)); // Duplicate page while URL is pending.
    cJSON* resolved = cJSON_Parse(R"({"code":"PlayControl","general":{"action":"refresh_play_url","data":{"success":true,"preTtsFlag":false,"items":[{"audioId":"a","success":true,"format":"mp3","url":"https://test/a.mp3"}]}}})");
    assert(player.HandleSkillCard(resolved));
    assert((player.pending_urls_ == std::vector<std::string>{"https://test/a.mp3"}));
    assert(player.request_generation_ == generation + 1 && player.auto_next_deadline_ms_ == 0);
    assert(!player.catalog_.HasNext() && !player.HandleSkillCard(resolved));

    for (int cancellation = 0; cancellation < 5; ++cancellation) {
        player.Stop(); player.catalog_.Reset(); player.auto_next_ready_ = true;
        assert(!player.PrepareCloudRequest(player.request_generation_, 1000).empty());
        if (cancellation == 0) player.BeginTurn();
        if (cancellation == 1) player.NotifyTtsStarted();
        if (cancellation == 2) player.Stop();
        if (cancellation == 3) test_clock_us = 31000000;
        if (cancellation == 4) {
            player.Shutdown(); player.running_ = true; // Service restarted before a late cloud response.
        }
        assert(!player.HandleSkillCard(page) && player.pending_urls_.empty());
        assert(!player.catalog_.HasNext());
        test_clock_us = 0;
    }
    cJSON_Delete(resolved);
    cJSON_Delete(page);
}
