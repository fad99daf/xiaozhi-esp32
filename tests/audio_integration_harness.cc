// Integration harness: actual TuyaProtocol::HandleAudio + Application
// OnIncomingJson/OnIncomingAudio callbacks + AudioService queue/abort methods,
// wired exactly as on device (C3: blocking decode push from the TAI receive
// thread, all protocol callbacks serialized on ctrl_mutex_).
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <deque>
#include <functional>
#include <future>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
using namespace std::chrono_literals;

#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGD(...) ((void)0)
#define pdMS_TO_TICKS(ms) (ms)

// AudioService constants (C3 profile: tiny blocking decode queue).
constexpr int MAX_DECODE_PACKETS_IN_QUEUE = 2;
constexpr int TAI_OPUS_FRAME_SIZE_BYTES = 80, TAI_OPUS_FRAME_DURATION_MS = 40;
// TAI stream flags (tuya_ai.h); only the values HandleAudio branches on.
constexpr uint8_t TAI_STREAM_ONE_SHOT = 0x00, TAI_STREAM_START = 0x01;

// A real condition_variable that signals a promise when a producer blocks.
std::promise<void>* full_queue = nullptr;
struct TestCV : std::condition_variable {
    template<class Lock, class Predicate> void wait(Lock& lock, Predicate predicate) {
        if (full_queue && !predicate()) { full_queue->set_value(); full_queue = nullptr; }
        std::condition_variable::wait(lock, predicate);
    }
};

struct cJSON {
    const char* valuestring = nullptr;
    std::map<std::string, cJSON> fields;
};
cJSON* cJSON_GetObjectItem(const cJSON* root, const char* key) {
    auto it = root->fields.find(key);
    return it == root->fields.end() ? nullptr : const_cast<cJSON*>(&it->second);
}
bool cJSON_IsString(const cJSON* item) { return item && item->valuestring; }
bool cJSON_IsObject(const cJSON* item) { return item && !item->valuestring; }

enum DeviceState { kDeviceStateUnknown, kDeviceStateIdle, kDeviceStateConnecting,
    kDeviceStateListening, kDeviceStateSpeaking, kDeviceStateWifiConfiguring,
    kDeviceStateActivating, kDeviceStateAudioTesting };
enum ListeningMode { kListeningModeAutoStop, kListeningModeManualStop, kListeningModeRealtime };
namespace Lang {
namespace Strings { const char *ERROR = "error", *SPEAKING = "speaking"; }
namespace Sounds { const char *OGG_POPUP = "popup", *OGG_VIBRATION = "vibration"; }
}
struct Display {
    void SetChatMessage(const char*, const char*) {}
    void SetStatus(const char*) {}
    void SetEmotion(const char*) {}
    void ClearChatMessages() {}
};
struct Codec {
    void ClearOutputBuffer() {}
    void EnableOutput(bool) {}
    int output_sample_rate() { return 16000; }
};
struct Led { void OnStateChanged() {} };
struct Board {
    Display display;
    Codec codec;
    Led led;
    static Board& GetInstance() { static Board board; return board; }
    Display* GetDisplay() { return &display; }
    Codec* GetAudioCodec() { return &codec; }
    Led* GetLed() { return &led; }
};
struct McpServer {
    static McpServer& GetInstance() { static McpServer server; return server; }
    void ParseMessage(const cJSON*) {}
};
void xEventGroupSetBits(void*, int) {}
void esp_timer_stop(int) {}
void esp_timer_start_periodic(int, int) {}
void esp_opus_dec_reset(void*) {}

struct AudioStreamPacket {
    int sample_rate = 16000, frame_duration = 40;
    uint32_t timestamp = 1;
    std::vector<uint8_t> payload;
};
struct DecodeAudioPacket : AudioStreamPacket {};

struct AudioService {
    Codec codec;
    Codec* codec_ = &codec;
    std::mutex audio_queue_mutex_;
    TestCV audio_queue_cv_;
    bool service_stopped_ = false;
    std::atomic<bool> output_aborted_{false};
    std::atomic<uint32_t> output_generation_{0};
    std::deque<std::unique_ptr<DecodeAudioPacket>> audio_decode_queue_;
    std::deque<std::unique_ptr<AudioStreamPacket>> audio_send_queue_, audio_testing_queue_;
    std::deque<uint32_t> timestamp_queue_;
    void* opus_decoder_ = (void*)1;
    std::mutex decoder_mutex_;
    std::deque<int> audio_playback_queue_, audio_encode_queue_;  // size probes only
    void CancelDrainWait() {}
    void AbortOutput();
    void ResetDecoder();
    uint32_t OutputGeneration() const { return output_generation_.load(); }
    bool PushPacketToDecodeQueue(std::unique_ptr<AudioStreamPacket>, bool);
};
// AUDIO_SERVICE_METHODS

class Application {
public:
    std::mutex mutex_;
    std::deque<std::function<void()>> main_tasks_;
    std::atomic<bool> aborted_{false};
    std::mutex tts_mutex_;
    uint64_t tts_generation_ = 0;
    DeviceState state_ = kDeviceStateListening;
    ListeningMode listening_mode_ = kListeningModeAutoStop;
    bool tts_start_pending_ = false;
    AudioService audio_service_;
    std::atomic<int> rejected_{0};

    // Registration target for the extracted callback-registration statements:
    // protocol_->OnIncomingX(cb) stores the callback for later invocation.
    Application* protocol_ = this;
    std::function<void(const cJSON*)> on_json;
    std::function<void(std::unique_ptr<AudioStreamPacket>)> on_audio;
    void OnIncomingJson(std::function<void(const cJSON*)> cb) { on_json = std::move(cb); }
    void OnIncomingAudio(std::function<void(std::unique_ptr<AudioStreamPacket>)> cb) {
        on_audio = std::move(cb);
    }

    DeviceState GetDeviceState() { return state_; }
    void SetDeviceState(DeviceState state) { state_ = state; }
    void Register() {
        auto display = Board::GetInstance().GetDisplay();
        // APPLICATION_CALLBACKS
    }
    // Thin re-dispatchers so the test can invoke the registered callbacks and
    // HandleAudio can re-enter the audio callback after each push returns.
    void HandleIncomingJson(const cJSON* root) { on_json(root); }
    void CheckIncomingAudio(std::unique_ptr<AudioStreamPacket> packet) {
        on_audio(std::move(packet));
    }
    void Schedule(std::function<void()>&& callback) {
        std::lock_guard<std::mutex> lock(mutex_);
        main_tasks_.push_back(std::move(callback));
    }
    void Pump() {
        for (;;) {
            std::function<void()> task;
            {
                std::lock_guard<std::mutex> lock(mutex_);
                if (main_tasks_.empty()) return;
                task = std::move(main_tasks_.front());
                main_tasks_.pop_front();
            }
            task();
        }
    }
    // Synchronous CancelTtsLocked so the test does not need AbortSpeaking's
    // protocol call: identical audio invalidation semantics.
    void CancelTts() {
        std::lock_guard<std::mutex> lock(tts_mutex_);
        ++tts_generation_;
        aborted_ = true;
        audio_service_.AbortOutput();
        audio_service_.CancelDrainWait();
        Board::GetInstance().GetAudioCodec()->ClearOutputBuffer();
    }
    // Stubs for branches of the extracted callbacks this scenario never takes.
    void Alert(const char*, const char*, const char*, const char*) { assert(false); }
    void Reboot() { assert(false); }
    void AwaitTtsDrain(uint64_t) { assert(false); }
    uint64_t CancelTtsLocked() { assert(false); return 0; }
};

struct TuyaProtocol {
    std::mutex ctrl_mutex_;
    uint64_t interrupt_time_ms_ = 0;
    bool response_receiving_ = false;
    int audio_recv_count_ = 0, turn_count_ = 0;
    bool first_tts_audio_pending_ = false;
    int server_sample_rate_ = 16000, server_frame_duration_ = 40;
    std::vector<uint8_t> audio_reassembly_buf_;
    std::function<void(std::unique_ptr<AudioStreamPacket>)> on_incoming_audio_;
    void HandleAudio(const uint8_t*, size_t, uint32_t, uint16_t, uint8_t, uint64_t);
};
// PROTOCOL_METHODS

int main() {
    Application app;
    app.Register();
    TuyaProtocol protocol;
    protocol.on_incoming_audio_ = [&](std::unique_ptr<AudioStreamPacket> packet) {
        // Re-run the registered callback: HandleAudio invokes the callback
        // AFTER each push returns, so an invalidated in-flight push falls
        // through to here and re-evaluates Application state.
        app.CheckIncomingAudio(std::move(packet));
    };

    // Turn 1: speaking, decode queue full; the receive thread blocks in the
    // blocking push while holding ctrl_mutex_ (the C3 barge-in backpressure).
    app.SetDeviceState(kDeviceStateSpeaking);
    for (int i = 0; i < MAX_DECODE_PACKETS_IN_QUEUE; ++i) {
        assert(app.audio_service_.PushPacketToDecodeQueue(
            std::make_unique<AudioStreamPacket>(), false));
    }
    std::vector<uint8_t> opus(TAI_OPUS_FRAME_SIZE_BYTES, 9);
    std::promise<void> waiting;
    full_queue = &waiting;
    auto receive = std::async(std::launch::async, [&] {
        protocol.HandleAudio(opus.data(), opus.size(), 16000, 40, TAI_STREAM_ONE_SHOT,
                             1790000001000);
    });
    assert(waiting.get_future().wait_for(2s) == std::future_status::ready);

    // Main loop: user interrupts (abort), then the next turn's tts/start is
    // pumped — ResetDecoder releases the blocked push, aborted_ is cleared.
    app.CancelTts();
    assert(app.aborted_);
    {
        cJSON root;
        root.fields["type"].valuestring = "tts";
        root.fields["state"].valuestring = "start";
        app.HandleIncomingJson(&root);
    }
    app.Pump();
    assert(app.GetDeviceState() == kDeviceStateSpeaking && !app.aborted_);
    receive.get();

    // The cancelled turn's in-flight packet is discarded because its captured
    // queue generation changed. It must neither enter the new turn's queue nor
    // be delivered to a later live-turn callback.
    assert(app.audio_service_.audio_decode_queue_.size() == 0);
    assert(app.rejected_ == 0);

    std::cout << "blocked push cannot reenqueue after next turn start\n";
}
