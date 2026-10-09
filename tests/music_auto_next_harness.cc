#include <atomic>
#include <cassert>
#include <cstdint>
#include <functional>
#include <mutex>
#include <string>
#include <vector>
#include <ctime>
#include "music_playback_state.h"
#include "music_catalog.h"

#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define pdTRUE 1
#define portMAX_DELAY 0
#define pdMS_TO_TICKS(x) (x)
using TickType_t = uint32_t;
unsigned heap_caps_get_free_size(int) { return 0; }
#define MALLOC_CAP_INTERNAL 0
TickType_t xTaskGetTickCount() { return 0; }

struct MusicPlayer;
MusicPlayer* current;
int notifications;
void ulTaskNotifyTake(int, unsigned);

struct MusicPlayer {
    std::atomic<bool> running_{true};
    std::atomic<uint32_t> request_generation_{7};
    std::mutex request_mutex_;
    std::vector<std::string> pending_urls_{"first", "second"};
    MusicStartGate gate_;
    MusicStartGate::Ticket pending_ticket_{false, 0};
    MusicPlaybackState playback_state_;
    std::function<void()> wait_for_output_;
    std::function<uint32_t()> begin_playback_;
    std::function<void(uint32_t)> cloud_request_ready_;
    MusicCatalog catalog_;
    uint32_t cloud_request_sequence_ = 0;
    MusicPlayer() { catalog_.Reset(); }
    bool auto_next_enabled_ = true;
    bool music_playlist_ = true;
    bool auto_next_ready_ = false;
    uint64_t auto_next_deadline_ms_ = 0;
    int streamed = 0;
    int fail_on = -1;
    bool cancel = false;
    bool replace = false;
    std::vector<std::string> streams;
    void* task_ = nullptr;
    std::function<void()> cancel_output_, pause_output_;
    bool StreamMp3(const std::string& url, uint32_t, uint32_t) {
        streams.push_back(url);
        if (cancel) ++request_generation_;
        ++streamed;
        if (replace && streamed == 1) {
            ++request_generation_;
            pending_urls_ = {"replacement"}; // Its notification was consumed inside the old stream.
            return false;
        }
        return streamed != fail_on;
    }
    void TaskLoop();
    bool IsCancelled(uint32_t) const;
    std::string PrepareCloudRequest(uint32_t, uint64_t);
    bool CanPublishAutoNext(uint32_t, uint64_t);
    bool ExpireAutoNext(uint64_t);
    void CancelAutoNext();
    void CancelCloudRequestLocked();
    void BeginTurn();
    void Stop();
};

void ulTaskNotifyTake(int, unsigned) {
    if (++notifications > 1) current->running_ = false;
}
void xTaskNotifyGive(void*) {}

constexpr int OPRT_OK = 0;
int published;
int iot_client_publish(void*, const uint8_t*, size_t) { ++published; return OPRT_OK; }
struct TuyaProtocol {
    std::mutex mqtt_request_mutex_;
    std::string pending_music_request_;
    std::function<bool()> pending_music_request_valid_;
    std::atomic<bool> mqtt_pump_running_{true};
    void PublishPending();
};

// WORKER_METHODS

int main() {
    for (int scenario = 0; scenario < 7; ++scenario) {
        MusicPlayer player;
        current = &player;
        notifications = 0;
        int drained = 0, completed = 0;
        player.wait_for_output_ = [&] {
            ++drained;
            if (scenario == 5 && drained == 2) player.playback_state_.PauseForTurn();
        };
        player.cloud_request_ready_ = [&](uint32_t generation) {
            assert(drained == 2 && generation == 7);
            ++completed;
        };
        if (scenario == 1) player.auto_next_enabled_ = false;
        if (scenario == 2) player.fail_on = 2;
        if (scenario == 3) player.cancel = true;
        if (scenario == 4) player.music_playlist_ = false;
        if (scenario == 6) {
            player.catalog_.Request("page", 1000); player.catalog_.CancelPending();
        }
        player.TaskLoop();
        assert(completed == (scenario == 0 ? 1 : 0));
        if (scenario == 0) assert(player.streamed == 2 && player.auto_next_ready_);
    }
    MusicPlayer replacement;
    current = &replacement; notifications = 0;
    replacement.replace = true;
    int replacement_requests = 0;
    replacement.cloud_request_ready_ = [&](uint32_t generation) {
        assert(generation == 8); ++replacement_requests;
    };
    replacement.TaskLoop();
    assert((replacement.streams == std::vector<std::string>{"first", "replacement"}));
    assert(replacement_requests == 1);
    MusicPlayer player;
    player.auto_next_ready_ = true;
    assert(player.PrepareCloudRequest(6, 1000).empty());
    assert(!player.PrepareCloudRequest(7, 1000).empty());
    assert(player.PrepareCloudRequest(7, 1001).empty());  // At most one request.
    assert(!player.ExpireAutoNext(30999));
    assert(player.ExpireAutoNext(31000));
    assert(!player.ExpireAutoNext(31001));   // Timeout never retries.
    player.auto_next_ready_ = true;
    player.BeginTurn();
    assert(player.PrepareCloudRequest(player.request_generation_, 40000).empty());
    player.auto_next_ready_ = true;
    player.Stop();
    assert(player.PrepareCloudRequest(player.request_generation_, 40000).empty());
    player.auto_next_ready_ = true;
    player.CancelAutoNext();
    assert(player.PrepareCloudRequest(player.request_generation_, 40000).empty());

    for (int scenario = 0; scenario < 4; ++scenario) {
        MusicPlayer pending_player;
        pending_player.auto_next_ready_ = true;
        assert(!pending_player.PrepareCloudRequest(7, 1000).empty());
        uint64_t now = 1001;
        TuyaProtocol transport;
        transport.pending_music_request_ = "next";
        transport.pending_music_request_valid_ = [&] {
            return pending_player.CanPublishAutoNext(7, now);
        };
        if (scenario == 1) pending_player.Stop();
        if (scenario == 2) now = 31000;  // Pump delayed past deadline, even without clock tick.
        if (scenario == 3) pending_player.BeginTurn();
        published = 0;
        transport.PublishPending();
        assert(published == (scenario == 0 ? 1 : 0));
        transport.PublishPending();
        assert(published == (scenario == 0 ? 1 : 0));  // Never retry consumed request.
    }
}
