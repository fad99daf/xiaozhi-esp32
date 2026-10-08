#include <atomic>
#include <cassert>
#include <cstdint>
#include <functional>
#include <mutex>
#include <string>
#include <vector>
#include "music_playback_state.h"

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
    std::function<void(uint32_t)> playback_finished_;
    bool auto_next_enabled_ = true;
    bool music_playlist_ = true;
    bool auto_next_ready_ = false;
    uint64_t auto_next_deadline_ms_ = 0;
    int streamed = 0;
    int fail_on = -1;
    bool cancel = false;
    void* task_ = nullptr;
    std::function<void()> cancel_output_, pause_output_;
    bool StreamMp3(const std::string&, uint32_t, uint32_t) {
        if (cancel) ++request_generation_;
        return ++streamed != fail_on;
    }
    void TaskLoop();
    bool IsCancelled(uint32_t) const;
    bool ConsumeAutoNext(uint32_t, uint64_t);
    bool ExpireAutoNext(uint64_t);
    void CancelAutoNext();
    void BeginTurn();
    void Stop();
};

void ulTaskNotifyTake(int, unsigned) {
    if (++notifications > 1) current->running_ = false;
}
void xTaskNotifyGive(void*) {}

// WORKER_METHODS

int main() {
    for (int scenario = 0; scenario < 6; ++scenario) {
        MusicPlayer player;
        current = &player;
        notifications = 0;
        int drained = 0, completed = 0;
        player.wait_for_output_ = [&] {
            ++drained;
            if (scenario == 5 && drained == 2) player.playback_state_.PauseForTurn();
        };
        player.playback_finished_ = [&](uint32_t generation) {
            assert(drained == 2 && generation == 7);
            ++completed;
        };
        if (scenario == 1) player.auto_next_enabled_ = false;
        if (scenario == 2) player.fail_on = 2;
        if (scenario == 3) player.cancel = true;
        if (scenario == 4) player.music_playlist_ = false;
        player.TaskLoop();
        assert(completed == (scenario == 0 ? 1 : 0));
        if (scenario == 0) assert(player.streamed == 2 && player.auto_next_ready_);
    }
    MusicPlayer player;
    player.auto_next_ready_ = true;
    assert(!player.ConsumeAutoNext(6, 1000));
    assert(player.ConsumeAutoNext(7, 1000));
    assert(!player.ConsumeAutoNext(7, 1001));  // At most one next request.
    assert(!player.ExpireAutoNext(30999));
    assert(player.ExpireAutoNext(31000));
    assert(!player.ExpireAutoNext(31001));   // Timeout never retries.
    player.auto_next_ready_ = true;
    player.BeginTurn();
    assert(!player.ConsumeAutoNext(player.request_generation_, 40000));
    player.auto_next_ready_ = true;
    player.Stop();
    assert(!player.ConsumeAutoNext(player.request_generation_, 40000));
    player.auto_next_ready_ = true;
    player.CancelAutoNext();
    assert(!player.ConsumeAutoNext(player.request_generation_, 40000));
}
