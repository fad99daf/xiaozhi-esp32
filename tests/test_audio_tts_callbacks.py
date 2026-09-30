"""Run extracted Application callbacks with deterministic main/task queues.

No ESP-IDF or hardware required; uses a C++17 host compiler with ASan/UBSan.
"""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def block(source, marker):
    start = source.index(marker)
    opening = source.index('{', start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


HARNESS = r'''
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <deque>
#include <functional>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <new>
#include <string>
#include <thread>
#include <vector>

#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGD(...) ((void)0)
#define pdMS_TO_TICKS(ms) (ms)
#define pdPASS 1
#define MAIN_EVENT_SCHEDULE 1
using TaskHandle_t = void*;
using EventGroupHandle_t = void*;
void xEventGroupSetBits(void*, int) {}
std::thread::id main_thread;
void vTaskDelete(void*) {}
void vTaskDelay(int ms) { assert(ms == 50); }
struct Task { void (*fn)(void*); void* arg; TaskHandle_t handle; };
std::deque<Task> workers;
bool fail_task = false;
uintptr_t next_handle = 0;
int xTaskCreate(void (*fn)(void*), const char*, int, void* arg, int, TaskHandle_t* handle) {
    assert(std::this_thread::get_id() == main_thread);
    if (fail_task) return 0;
    *handle = reinterpret_cast<void*>(++next_handle);
    workers.push_back({fn, arg, *handle});
    return pdPASS;
}
void RunWorker() {
    assert(!workers.empty());
    auto task = workers.front();
    workers.pop_front();
    std::thread worker([task] { task.fn(task.arg); });
    worker.join();
}

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
enum AbortReason { kAbortReasonNone };
enum class PowerSaveLevel { LOW_POWER };
namespace Lang {
namespace Strings {
const char *ERROR = "error", *STANDBY = "standby", *CONNECTING = "connecting",
           *LISTENING = "listening", *SPEAKING = "speaking";
}
namespace Sounds { const char *OGG_EXCLAMATION = "error", *OGG_POPUP = "popup", *OGG_VIBRATION = "vibration"; }
}
struct Display {
    std::string assistant, status;
    std::function<void()> on_message;
    void SetChatMessage(const char* role, const char* text) {
        if (std::string(role) == "assistant") {
            if (on_message) on_message();
            assistant = text;
        }
    }
    void SetStatus(const char* text) { status = text; }
    void SetEmotion(const char*) {}
    void ClearChatMessages() { assistant.clear(); }
    void UpdateStatusBar(bool) {}
};
struct Codec { int clears = 0; void ClearOutputBuffer() { ++clears; } };
struct Led { void OnStateChanged() {} };
struct Board {
    Display display;
    Codec codec;
    Led led;
    static Board& GetInstance() { static Board board; return board; }
    Display* GetDisplay() { return &display; }
    Codec* GetAudioCodec() { return &codec; }
    Led* GetLed() { return &led; }
    void SetPowerSaveLevel(PowerSaveLevel) {}
};
struct AudioStreamPacket {};
struct AudioService {
    int aborts = 0, resets = 0, waits = 0, flushes = 0, packets = 0;
    bool output_aborted = false, processing = true;
    std::function<void()> on_reset;
    void AbortOutput() { ++aborts; output_aborted = true; }
    void CancelDrainWait() {}
    void FlushAudioQueues() { ++flushes; }
    void ResetDecoder() { if (on_reset) on_reset(); ++resets; output_aborted = false; }
    void WaitForPlaybackQueueEmpty() { ++waits; }
    void EnableAudioTesting(bool) {}
    void EnableVoiceProcessing(bool value) { processing = value; }
    void EnableWakeWordDetection(bool) {}
    bool IsAudioProcessorRunning() { return processing; }
    bool IsAfeWakeWord() { return true; }
    void PlaySound(const char*) {}
    uint32_t OutputGeneration() const { return output_generation_; }
    bool PushPacketToDecodeQueue(std::unique_ptr<AudioStreamPacket>, bool) {
        ++packets; return true;
    }
    uint32_t output_generation_ = 0;
};
struct Protocol {
    std::function<void(const cJSON*)> json;
    std::function<void(const std::string&)> error;
    std::function<void()> closed, on_call;
    std::function<void(std::unique_ptr<AudioStreamPacket>)> audio;
    void OnIncomingJson(decltype(json) cb) { json = cb; }
    void OnNetworkError(decltype(error) cb) { error = cb; }
    void OnAudioChannelClosed(decltype(closed) cb) { closed = cb; }
    void OnIncomingAudio(decltype(audio) cb) { audio = cb; }
    int abort_calls = 0;
    bool response_cancelled = false;
    bool IsAudioChannelOpened() { return true; }
    void SendAbortSpeaking(AbortReason) {
        ++abort_calls;
        response_cancelled = true;
        if (on_call) on_call();
    }
    // Simplified CancelTurn(false): no abort callback, and END is consumed silently.
    void ReceiveEnd() {
        if (response_cancelled) return;
        cJSON root;
        root.fields["type"].valuestring = "tts";
        root.fields["state"].valuestring = "stream_end";
        json(&root);
    }
    void SendStartListening(ListeningMode) { if (on_call) on_call(); }
    void CloseAudioChannel() { if (on_call) on_call(); closed(); }
};
struct McpServer {
    static McpServer& GetInstance() { static McpServer server; return server; }
    void ParseMessage(const cJSON*) {}
};
struct StateMachine {
    DeviceState state = kDeviceStateListening;
    DeviceState GetState() { return state; }
};
class Application {
public:
    // APPLICATION_FIELDS
    StateMachine state_machine_;
    ListeningMode listening_mode_ = kListeningModeAutoStop;
    std::function<void()> on_transition;
    int alerts = 0, clock_ticks_ = 0;
    bool play_popup_on_listening_ = false;
    Application() {
        protocol_ = std::make_unique<Protocol>();
        Register();
    }
    DeviceState GetDeviceState() { return state_machine_.GetState(); }
    bool SetDeviceState(DeviceState state) {
        assert(std::this_thread::get_id() == main_thread);
        if (on_transition) on_transition();
        state_machine_.state = state;
        return true;
    }
    void Alert(const char*, const char*, const char*, const char*) { ++alerts; }
    void Reboot() {}
    void Register() {
        auto& board = Board::GetInstance();
        auto display = board.GetDisplay();
        // APPLICATION_CALLBACKS
    }
    void Schedule(std::function<void()>&& callback);
    uint64_t CancelTtsLocked();
    void AbortSpeaking(AbortReason reason);
    void AwaitTtsDrain(uint64_t generation);
    void HandleStateChangedEvent();
    void HandleNetworkDisconnectedEvent();
    void HandleToggleChatEvent();
    void HandleStartListeningEvent();
    void SetListeningMode(ListeningMode mode);
    ListeningMode GetDefaultListeningMode() { return kListeningModeAutoStop; }
    void ContinueOpenAudioChannel(ListeningMode) { assert(false); }
    void Tts(const char* state, const char* text = "old sentence") {
        cJSON root;
        root.fields["type"].valuestring = "tts";
        root.fields["state"].valuestring = state;
        root.fields["text"].valuestring = text;
        protocol_->json(&root);
    }
    std::function<void()> Take() {
        std::lock_guard<std::mutex> lock(mutex_);
        assert(!main_tasks_.empty());
        auto task = std::move(main_tasks_.front());
        main_tasks_.pop_front();
        return task;
    }
    void Pump() {
        while (!main_tasks_.empty()) Take()();
    }
    void Start() {
        Tts("start"); Pump();
        assert(GetDeviceState() == kDeviceStateSpeaking && !aborted_);
    }
    void CheckLock(bool held) {
        bool acquired = false;
        std::thread probe([&] {
            acquired = tts_mutex_.try_lock();
            if (acquired) tts_mutex_.unlock();
        });
        probe.join();
        assert(acquired != held);
    }
};
// APPLICATION_METHODS

int main(int argc, char** argv) {
    assert(argc == 2);
    main_thread = std::this_thread::get_id();
    Application app;
    auto& display = Board::GetInstance().display;
    std::string test = argv[1];
    if (test == "cancel_before_start") {
        app.Tts("start"); app.Tts("sentence_start"); app.Tts("stop");
        app.Tts("abort");
        assert(app.aborted_ && app.audio_service_.aborts == 1);
        assert(Board::GetInstance().codec.clears == 1);
        app.Pump();
        assert(app.GetDeviceState() == kDeviceStateListening);
        assert(app.aborted_ && app.audio_service_.resets == 0);
        assert(display.assistant.empty());
    } else if (test == "cancel_before_state_handling") {
        app.Start();
        app.Tts("abort");
        app.HandleStateChangedEvent();
        assert(app.aborted_ && app.audio_service_.output_aborted);
        assert(app.audio_service_.resets == 1);
        assert(app.audio_service_.processing);
        app.Pump();
    } else if (test == "old_abort_new_start") {
        app.Start();
        app.Tts("abort");
        auto stale = app.Take();
        app.Start();
        stale();
        assert(!app.aborted_ && app.GetDeviceState() == kDeviceStateSpeaking);
    } else if (test == "old_stop_new_start") {
        app.Start();
        app.Tts("stop");
        auto stale = app.Take();
        app.Start(); stale();
        assert(app.GetDeviceState() == kDeviceStateSpeaking);
    } else if (test == "old_sentence_new_start") {
        app.Start();
        app.Tts("sentence_start");
        auto stale = app.Take();
        app.Tts("abort"); app.Start();
        app.Tts("sentence_start", "new sentence"); app.Pump(); stale();
        assert(display.assistant == "new sentence");
    } else if (test == "channel_close") {
        app.Tts("start"); app.Tts("sentence_start");
        app.protocol_->closed();
        assert(app.aborted_ && app.audio_service_.output_aborted);
        app.Pump();
        assert(app.GetDeviceState() == kDeviceStateIdle && display.assistant.empty());
        app.protocol_->closed();
        auto stale = app.Take();
        app.Start(); stale();
        assert(app.GetDeviceState() == kDeviceStateSpeaking);
    } else if (test == "network_error") {
        app.Tts("start"); app.Tts("sentence_start");
        app.protocol_->error("disconnected"); app.Pump();
        assert(app.aborted_ && app.alerts == 1 && display.assistant.empty());
        assert(app.GetDeviceState() == kDeviceStateIdle && app.audio_service_.flushes == 1);
        app.protocol_->error("old error");
        auto stale = app.Take();
        app.Start(); stale();
        assert(app.GetDeviceState() == kDeviceStateSpeaking && app.alerts == 1);
    } else if (test == "local_abort" || test == "toggle_abort_without_callbacks") {
        for (auto mode : {kListeningModeAutoStop, kListeningModeManualStop, kListeningModeRealtime}) {
            app.listening_mode_ = mode;
            app.Start(); app.HandleStateChangedEvent();
            app.Tts("sentence_start");
            app.protocol_->on_call = [&] { app.CheckLock(false); };
            auto generation = app.tts_generation_;
            auto aborts = app.protocol_->abort_calls;
            if (test == "local_abort") app.AbortSpeaking(kAbortReasonNone);
            else app.HandleToggleChatEvent();
            assert(app.tts_generation_ == generation + 1);
            assert(app.protocol_->abort_calls == aborts + 1);
            assert(app.aborted_ && app.audio_service_.output_aborted);
            assert(app.GetDeviceState() == kDeviceStateSpeaking);
            app.Pump();
            auto expected = mode == kListeningModeManualStop ? kDeviceStateIdle : kDeviceStateListening;
            assert(app.GetDeviceState() == expected);
            assert(display.assistant.empty() && workers.empty());
            app.HandleStateChangedEvent();
            assert(app.audio_service_.processing == (expected == kDeviceStateListening));
            // The transition already happened during silence, before any END.
            app.protocol_->ReceiveEnd(); app.Pump();
            assert(app.GetDeviceState() == expected && workers.empty());
        }
        assert(app.audio_service_.resets == 3 && app.audio_service_.aborts == 3);
        assert(Board::GetInstance().codec.clears == 3);
    } else if (test == "local_abort_before_start") {
        app.Tts("start"); app.Tts("sentence_start"); app.Tts("stop");
        app.AbortSpeaking(kAbortReasonNone); app.Pump();
        assert(app.GetDeviceState() == kDeviceStateListening);
        assert(app.aborted_ && display.assistant.empty() && app.audio_service_.resets == 0);
    } else if (test == "local_abort_new_start") {
        app.Start(); app.AbortSpeaking(kAbortReasonNone);
        auto stale = app.Take();
        app.Tts("start");
        // Even a start whose main-loop action is still pending invalidates the abort.
        stale();
        assert(app.GetDeviceState() == kDeviceStateSpeaking);
        app.Pump(); stale();
        assert(!app.aborted_ && app.GetDeviceState() == kDeviceStateSpeaking);
    } else if (test == "local_abort_preserves_immediate_listening") {
        for (auto mode : {kListeningModeAutoStop, kListeningModeManualStop, kListeningModeRealtime}) {
            app.Start(); app.AbortSpeaking(kAbortReasonNone);
            app.SetListeningMode(mode);
            app.on_transition = [] { assert(false); };
            app.Pump();
            assert(app.GetDeviceState() == kDeviceStateListening && app.listening_mode_ == mode);
            app.on_transition = nullptr;
        }
        app.Start(); app.HandleStartListeningEvent();
        app.on_transition = [] { assert(false); };
        app.Pump();
        assert(app.GetDeviceState() == kDeviceStateListening);
        assert(app.listening_mode_ == kListeningModeManualStop);
    } else if (test == "local_abort_preserves_other_state") {
        app.Start(); app.AbortSpeaking(kAbortReasonNone);
        app.SetDeviceState(kDeviceStateIdle);
        app.on_transition = [] { assert(false); };
        app.Pump();
        assert(app.GetDeviceState() == kDeviceStateIdle);
    } else if (test == "local_abort_legacy_reentry") {
        app.Start();
        app.protocol_->on_call = [&] {
            app.CheckLock(false);
            // Synchronous callback reentry must not deadlock.
            app.Tts("abort");
        };
        app.AbortSpeaking(kAbortReasonNone); app.Pump();
        assert(app.aborted_ && app.GetDeviceState() == kDeviceStateListening);
    } else if (test == "local_abort_legacy_stop") {
        app.Start(); app.AbortSpeaking(kAbortReasonNone);
        app.Tts("stop"); app.Pump();
        assert(app.aborted_ && app.GetDeviceState() == kDeviceStateListening);
        app.Tts("stop"); app.Pump();
        assert(app.GetDeviceState() == kDeviceStateListening);
    } else if (test == "network_disconnect") {
        app.Tts("start");
        app.protocol_->on_call = [&] { app.CheckLock(false); };
        app.HandleNetworkDisconnectedEvent(); app.Pump();
        assert(app.aborted_ && app.audio_service_.resets == 0);
        assert(app.GetDeviceState() == kDeviceStateIdle);
    } else if (test == "stale_stream_end") {
        app.Start(); app.Tts("stream_end");
        auto stale = app.Take();
        app.Tts("abort"); app.Start(); stale();
        assert(workers.empty());
        assert(app.GetDeviceState() == kDeviceStateSpeaking);
    } else if (test == "drain_after_cancel") {
        app.Start(); app.Tts("stream_end"); app.Pump();
        auto handle = app.tts_drain_task_handle_;
        RunWorker();
        assert(app.tts_drain_task_handle_ == handle);
        auto completion = app.Take();
        app.Tts("abort"); app.Start(); completion();
        assert(app.tts_drain_task_handle_ == nullptr);
        assert(app.GetDeviceState() == kDeviceStateSpeaking);
    } else if (test == "drain_cannot_clear_new_worker") {
        app.Start(); app.Tts("stream_end"); app.Pump();
        auto old = app.tts_drain_task_handle_;
        app.Tts("abort"); app.Start();
        app.Tts("stream_end"); app.Pump();
        auto current = app.tts_drain_task_handle_;
        assert(current && current != old && workers.size() == 2);
        RunWorker(); app.Pump();
        assert(app.tts_drain_task_handle_ == current);
        assert(app.GetDeviceState() == kDeviceStateSpeaking);
        RunWorker();
        assert(app.tts_drain_task_handle_ == current);
        app.Pump();
        assert(app.tts_drain_task_handle_ == nullptr);
        assert(app.GetDeviceState() == kDeviceStateListening);
    } else if (test == "drain_success_and_duplicate") {
        app.listening_mode_ = kListeningModeManualStop;
        app.Start(); app.Tts("stream_end"); app.Tts("stream_end"); app.Pump();
        assert(workers.size() == 1);
        RunWorker(); app.Pump();
        assert(app.GetDeviceState() == kDeviceStateIdle && !app.tts_drain_task_handle_);
    } else if (test == "drain_creation_failure") {
        app.Start(); fail_task = true;
        app.Tts("stream_end"); app.Pump();
        assert(app.aborted_ && !app.tts_drain_task_handle_ && workers.empty());
        assert(app.GetDeviceState() == kDeviceStateIdle);
        fail_task = false;
        app.Start(); app.Tts("stream_end"); app.Pump();
        assert(workers.size() == 1);
        RunWorker(); app.Pump();
        assert(app.GetDeviceState() == kDeviceStateListening);
    } else if (test == "compound_actions_locked") {
        app.on_transition = [&] { app.CheckLock(true); };
        app.audio_service_.on_reset = [&] { app.CheckLock(true); };
        display.on_message = [&] { app.CheckLock(true); };
        app.Start(); app.Tts("sentence_start"); app.Tts("stop"); app.Pump();
        app.Start(); app.Tts("abort"); app.Pump();
        app.Start(); app.AbortSpeaking(kAbortReasonNone); app.Pump();
        app.Start(); app.Tts("stream_end"); app.Pump(); RunWorker(); app.Pump();
        app.protocol_->closed(); app.Pump();
        app.protocol_->error("error"); app.Pump();
    } else if (test == "tts_start_audio_before_pump_rejected") {
        // Bug: tts/start only SCHEDULES the transition to speaking. On the
        // device, TAI EVT_START / NLG run ahead of the audio burst, so the
        // receive thread delivers Opus frames while the state change is still
        // queued on the main loop. OnIncomingAudio checks state synchronously,
        // sees "not speaking", and drops every frame of the utterance.
        app.Tts("start");
        app.protocol_->audio(std::make_unique<AudioStreamPacket>());
        assert(app.GetDeviceState() == kDeviceStateListening);
        // The queued speaking transition admits its audio before the main loop
        // pumps the scheduled start action; the first TTS frame is not dropped.
        assert(app.audio_service_.packets == 1);
        // Control: after Pump() applies the start action, audio is accepted.
        app.Pump();
        assert(app.GetDeviceState() == kDeviceStateSpeaking && !app.aborted_);
        assert(app.audio_service_.resets == 1);
        app.protocol_->audio(std::make_unique<AudioStreamPacket>());
        assert(app.audio_service_.packets == 2);
    } else if (test == "legacy_start_stop_and_audio") {
        for (auto mode : {kListeningModeAutoStop, kListeningModeManualStop, kListeningModeRealtime}) {
            app.listening_mode_ = mode;
            app.Start(); app.HandleStateChangedEvent();
            app.protocol_->audio(std::make_unique<AudioStreamPacket>());
            app.Tts("sentence_start", "legacy"); app.Pump();
            assert(display.assistant == "legacy");
            app.Tts("stop"); app.Pump();
            assert(app.GetDeviceState() == (mode == kListeningModeManualStop
                ? kDeviceStateIdle : kDeviceStateListening));
        }
        assert(app.audio_service_.packets == 3);
        app.Tts("abort");
        app.protocol_->audio(std::make_unique<AudioStreamPacket>());
        assert(app.audio_service_.packets == 3);
        app.Pump();
    } else { assert(false); }
    assert(workers.empty());
    std::cout << test << " passed\n";
}
'''


class AudioTtsCallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (ROOT / 'main/application.cc').read_text()
        header = (ROOT / 'main/application.h').read_text()
        methods = '\n'.join(block(source, signature) for signature in (
            'void Application::Schedule(',
            'uint64_t Application::CancelTtsLocked(',
            'void Application::AbortSpeaking(',
            'void Application::AwaitTtsDrain(',
            'void Application::HandleStateChangedEvent(',
            'void Application::HandleNetworkDisconnectedEvent(',
            'void Application::HandleToggleChatEvent(',
            'void Application::HandleStartListeningEvent(',
            'void Application::SetListeningMode(',
        ))
        callbacks = '\n'.join(block(source, 'protocol_->' + name + '(') + ');'
                              for name in ('OnIncomingJson', 'OnNetworkError',
                                           'OnAudioChannelClosed', 'OnIncomingAudio'))
        names = ('mutex_', 'main_tasks_', 'protocol_', 'event_group_', 'audio_service_',
                 'aborted_', 'tts_mutex_', 'tts_generation_', 'tts_start_pending_',
                 'tts_drain_task_handle_', 'tts_drain_generation_')
        fields = '\n'.join(next(line for line in header.split('private:', 1)[1].splitlines()
                                if name in line.split('//')[0].replace('{', ' ').replace(';', ' ').split())
                           for name in names)
        harness = HARNESS.replace('// APPLICATION_METHODS', methods)
        harness = harness.replace('// APPLICATION_CALLBACKS', callbacks)
        harness = harness.replace('// APPLICATION_FIELDS', fields)
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        path = Path(cls.temp.name)
        (path / 'test.cc').write_text(harness)
        cls.executables = []
        for name, flags in (('legacy', []), ('tuya_c3', ['-DCONFIG_PROTOCOL_TUYA=1',
                                                      '-DCONFIG_IDF_TARGET_ESP32C3=1'])):
            executable = path / name
            subprocess.run(['c++', '-std=c++17', '-pthread', '-O1', '-g',
                            '-fsanitize=address,undefined', *flags, str(path / 'test.cc'),
                            '-o', str(executable)], check=True, timeout=60)
            cls.executables.append(executable)

    def run_case(self, name):
        for executable in self.executables:
            with self.subTest(configuration=executable.name):
                result = subprocess.run([str(executable), name], text=True,
                                        capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def add_case(name):
    def test(self):
        self.run_case(name)
    setattr(AudioTtsCallbackTests, 'test_' + name, test)


for case in (
    'cancel_before_start', 'cancel_before_state_handling', 'old_abort_new_start',
    'old_stop_new_start', 'old_sentence_new_start', 'channel_close', 'network_error',
    'local_abort', 'toggle_abort_without_callbacks', 'local_abort_before_start',
    'local_abort_new_start', 'local_abort_preserves_immediate_listening',
    'local_abort_preserves_other_state', 'local_abort_legacy_reentry', 'local_abort_legacy_stop',
    'network_disconnect', 'stale_stream_end', 'drain_after_cancel',
    'drain_cannot_clear_new_worker', 'drain_success_and_duplicate',
    'drain_creation_failure', 'compound_actions_locked', 'legacy_start_stop_and_audio',
    'tts_start_audio_before_pump_rejected',
):
    add_case(case)


if __name__ == '__main__':
    unittest.main()
