"""Compile actual Tuya receive/control methods with real cJSON and host stubs."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_audio_abort import method

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / 'components/esp-agentic-kit/agentic-kit'
CJSON = SDK / 'third_party/cJSON'

HARNESS = r'''
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <functional>
#include <future>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
#include "cJSON.h"
// Stubbed TAI surface (tuya_ai.h): only the pieces the extracted methods use.
// Kept local so the harness does not track unrelated SDK header changes.
typedef struct tai_ctx tai_ctx_t;
struct tai_event_msg_t {
    uint16_t event_type;
    const uint8_t* data;
    size_t len;
    const uint8_t* user_data;
    size_t user_data_len;
};
constexpr int TAI_OK=0;
constexpr uint8_t TAI_STREAM_ONE_SHOT=0x00, TAI_STREAM_START=0x01, TAI_STREAM_MIDDLE=0x02;
constexpr uint16_t TAI_EVT_START=0, TAI_EVT_END=2, TAI_EVT_CHAT_BREAK=4,
    TAI_EVT_SERVER_VAD=5, TAI_EVT_MCP_CMD=1000;
constexpr uint8_t TAI_AUDIO_OPUS=111;
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
constexpr int TAI_OPUS_FRAME_SIZE_BYTES=80, TAI_OPUS_FRAME_DURATION_MS=40;
constexpr int OPRT_OK=0;
constexpr int MALLOC_CAP_INTERNAL=1, MALLOC_CAP_8BIT=2, MALLOC_CAP_SPIRAM=4;
using iot_client_t=int;
enum ListeningMode { kListeningModeAutoStop, kListeningModeManualStop, kListeningModeRealtime };
enum AbortReason { kAbortReasonNone };
struct AudioStreamPacket {
    std::vector<uint8_t> payload;
    int sample_rate, frame_duration;
};
struct AudioService {
    bool backpressured=false;
    bool IsDecodeQueueBackpressured() { return backpressured; }
};
struct Application {
    AudioService audio;
    std::vector<std::function<void()>> tasks;
    static Application& GetInstance() { static Application app; return app; }
    AudioService& GetAudioService() { return audio; }
    void Schedule(std::function<void()> f) { tasks.push_back(std::move(f)); }
};
std::function<void()> sdk_hook;
std::vector<std::string> mqtt_calls;
int sdk_breaks=0;
int tai_send_audio_start(tai_ctx_t*, uint8_t, uint8_t, uint8_t, uint32_t) { if (sdk_hook) sdk_hook(); return 0; }
int tai_chat_break(tai_ctx_t*) { if (sdk_hook) sdk_hook(); ++sdk_breaks; return 0; }
int tai_connect(tai_ctx_t*) { if (sdk_hook) sdk_hook(); return 0; }
void tai_disconnect(tai_ctx_t*) { if (sdk_hook) sdk_hook(); }
void iot_ai_ctrl_set_callback(iot_client_t*, void (*)(const char*, const char*, size_t, void*), void*) {
    mqtt_calls.push_back("register");
}
int iot_client_connect(iot_client_t*) { mqtt_calls.push_back("connect"); return 0; }
size_t heap_caps_get_free_size(int) { return 0; }
size_t heap_caps_get_minimum_free_size(int) { return 0; }
void log_heap_delta(const char*, size_t, size_t) {}
struct TuyaProtocol {
    std::mutex ctrl_mutex_, send_mutex_;
    bool realtime_mode_=false, response_receiving_=false;
    uint64_t interrupt_time_ms_=0;
    std::string last_interrupt_time_;
    bool has_received_first_nlg_=false, first_tts_audio_pending_=false;
    std::atomic<bool> is_first_audio_packet_{true}, audio_end_pending_{false};
    std::atomic<bool> session_active_{true}, connected_{true}, disconnect_cleanup_pending_{false};
    int audio_recv_count_=0, turn_count_=0;
    int server_sample_rate_=16000, server_frame_duration_=40;
    int connect_fail_count_=0;
    static constexpr int MAX_CONNECT_FAILS_BEFORE_REFRESH=2;
    std::vector<uint8_t> audio_reassembly_buf_, audio_batch_buf_;
    bool audio_batch_solo_next_=true;
    tai_ctx_t* ctx_=reinterpret_cast<tai_ctx_t*>(1);
    iot_client_t* iot_client_=reinterpret_cast<iot_client_t*>(1);
    std::function<void(std::unique_ptr<AudioStreamPacket>)> on_incoming_audio_;
    std::function<void(const cJSON*)> on_incoming_json_;
    std::function<void()> on_audio_channel_opened_, on_audio_channel_closed_;
    std::function<void(const std::string&)> on_network_error_;
    bool RefreshTaiContext() { return true; }
    void SetError(const std::string&) { assert(false); }
    bool StartMqttPump() { mqtt_calls.push_back("pump"); return true; }
    bool ConnectMqtt();
    static void OnEventCb(tai_ctx_t*, const tai_event_msg_t*, void*);
    static void OnAiCtrlCb(const char*, const char*, size_t, void*);
    static int OnFlowControlCb(tai_ctx_t*, void*);
    void HandleAiControl(const char*, const char*, size_t);
    bool AcceptInterruptTime(const char*, size_t, bool);
    void HandleEvent(uint16_t, const uint8_t*, size_t);
    void HandleText(const char*, size_t, uint8_t);
    void HandleAudio(const uint8_t*, size_t, uint32_t, uint16_t, uint8_t, uint64_t);
    void CancelTurn(bool);
    void ResetReceiveState();
    void SendAbortSpeaking(AbortReason);
    void SendStartListening(ListeningMode);
    bool OpenAudioChannel();
    void CloseAudioChannel(bool);
    void HandleDisconnect(uint8_t, uint8_t, uint16_t, uint8_t);
};
// METHODS
struct Fixture {
    TuyaProtocol p;
    int aborts=0, starts=0, sentences=0, stops=0, stream_ends=0, stt=0;
    std::vector<uint8_t> playback;
    // TTS streams on the wire: each response's audio arrives as one stream
    // whose first TAI packet carries TAI_STREAM_START, the rest MIDDLE (see
    // packet logs), all sharing the stream START's server timestamp_ms
    // (stream A: 1790000001000, B: 1790000002000, C: 1790000003000, ...).
    // Interruption timestamps sit between stream timestamps (e.g.
    // 1790000001500 interrupts A). Audio helpers emit frames for stream
    // `stream`; 0 = none.
    unsigned stream=1, stream_open_=1;
    Fixture() {
        sdk_hook=nullptr;
        Application::GetInstance().audio.backpressured=false;
        Application::GetInstance().tasks.clear();
        p.on_incoming_audio_=[this](std::unique_ptr<AudioStreamPacket> packet) {
            playback.insert(playback.end(), packet->payload.begin(), packet->payload.end());
        };
        p.on_incoming_json_=[this](const cJSON* root) {
            const char* type=cJSON_GetStringValue(cJSON_GetObjectItem(root, "type"));
            if (strcmp(type, "stt")==0) ++stt;
            const char* state=cJSON_GetStringValue(cJSON_GetObjectItem(root, "state"));
            if (!state) return;
            if (strcmp(state, "abort")==0) { ++aborts; playback.clear(); }
            if (strcmp(state, "start")==0) ++starts;
            if (strcmp(state, "sentence_start")==0) ++sentences;
            if (strcmp(state, "stop")==0 && strcmp(type, "tts")==0) ++stops;
            if (strcmp(state, "stream_end")==0) ++stream_ends;
        };
        p.SendStartListening(kListeningModeRealtime);
    }
    void event(int type, const std::string& attributes="", const std::string& payload="") {
        // Exact-sized buffers expose length-unaware reads to ASan.
        std::vector<uint8_t> attr(attributes.begin(), attributes.end());
        std::vector<uint8_t> data(payload.begin(), payload.end());
        tai_event_msg_t msg = {};
        msg.event_type = type;
        msg.data = data.empty() ? nullptr : data.data();
        msg.len = data.size();
        msg.user_data = attr.empty() ? nullptr : attr.data();
        msg.user_data_len = attr.size();
        TuyaProtocol::OnEventCb(nullptr, &msg, &p);
    }
    void mqtt(const std::string& json) {
        // No NUL terminator is accessible: ASan detects a length-unaware parse.
        std::vector<char> bytes(json.begin(), json.end());
        p.HandleAiControl("asrInterrupt", bytes.data(), bytes.size());
    }
    void interrupt(const std::string& time="1790000001500") {
        mqtt("{\"time\":\""+time+"\"}");
    }
    void tcp(const std::string& time="1790000001500") {
        event(TAI_EVT_CHAT_BREAK, "{\"breakAttributes\":{\"time\":\""+time+"\"}}");
    }
    // One HandleAudio call per 80-byte Opus frame, like the real wire: the
    // first frame of a response's TTS stream carries TAI_STREAM_START (see
    // packet logs), the rest TAI_STREAM_MIDDLE, and every frame carries the
    // stream START's server timestamp. A partial tail stays in the reassembly
    // buffer across calls, mirroring TCP chunking.
    uint64_t ts() const { return 1790000000000ull + 1000ull*stream; }
    void audio(size_t count=80, uint8_t value=7) {
        std::vector<uint8_t> bytes(count, value);
        size_t offset=0;
        while (offset < count) {
            size_t n=std::min(count-offset, (size_t)TAI_OPUS_FRAME_SIZE_BYTES);
            uint8_t flag=TAI_STREAM_MIDDLE;
            if (stream_open_!=stream) { flag=TAI_STREAM_START; stream_open_=stream; }
            p.HandleAudio(bytes.data()+offset, n, 16000, 40, flag, ts());
            offset+=n;
        }
    }
    // A header-only stream START (len==0): carries only the boundary's server
    // timestamp, no audio bytes.
    void header_start() {
        p.HandleAudio(nullptr, 0, 16000, 40, TAI_STREAM_START, ts());
        stream_open_=stream;
    }
    // The current stream has ended: the next audio() is a new stream START.
    void stream_end() { stream_open_=0; }
    void text(const std::string& json) { p.HandleText(json.data(), json.size(), 0); }
    void nlg() { text(R"({"bizType":"NLG","data":{"content":"hello"}})"); }
};
void receiving() {
    Fixture f;
    f.event(TAI_EVT_START);
    f.nlg();
    f.audio(97);
    assert(f.p.response_receiving_ && f.p.audio_reassembly_buf_.size()==17);
    Application::GetInstance().audio.backpressured=true;
    assert(TuyaProtocol::OnFlowControlCb(nullptr, &f.p)==0);
    f.interrupt();
    assert(f.aborts==1 && f.playback.empty() && f.p.audio_reassembly_buf_.empty());
    // The interrupted stream's timestamp becomes the cut-off: flow control
    // drains the connection instead of backpressuring it.
    assert(f.p.interrupt_time_ms_==1790000001500
           && TuyaProtocol::OnFlowControlCb(nullptr, &f.p)==1);
    // A's remaining MIDDLE frames (ts 1000 <= cut-off) are dropped even
    // though the decode queue is no longer backpressured.
    Application::GetInstance().audio.backpressured=false;
    f.audio(80, 3);
    assert(f.playback.empty() && TuyaProtocol::OnFlowControlCb(nullptr, &f.p)==1);
    f.stream=2;  // the interrupted stream is over; the next audio is a new stream
    f.event(TAI_EVT_START);
    f.nlg(); f.audio(8000);
    // The new turn's stream START carries a newer server timestamp (2000),
    // clearing the cut-off; NLG text is never discarded.
    assert(f.starts==2 && f.sentences==2 && f.playback.size()==8000);
    assert(f.p.interrupt_time_ms_==0 && TuyaProtocol::OnFlowControlCb(nullptr, &f.p)==1);
    f.text(R"({"bizType":"ASR","eof":1,"data":{"text":"new question"}})");
    assert(f.stt==1);
    f.p.SendStartListening(kListeningModeRealtime);
    assert(f.p.interrupt_time_ms_==0 && f.p.response_receiving_);
    f.stream_end();
    f.event(TAI_EVT_END);
    assert(!f.p.response_receiving_);
    assert(f.stops==1 && f.stream_ends==0);
    Application::GetInstance().audio.backpressured=true;
    assert(TuyaProtocol::OnFlowControlCb(nullptr, &f.p)==0);
    Application::GetInstance().audio.backpressured=false;
    f.stream=3;
    f.event(TAI_EVT_START); f.nlg(); f.audio(80, 9);
    assert(f.starts==3 && f.playback.size()==8080);
    assert(std::vector<uint8_t>(f.playback.end()-80, f.playback.end())==std::vector<uint8_t>(80, 9));
    f.event(TAI_EVT_END);
    assert(f.stops==2);
}
void dedup() {
    Fixture f;
    f.nlg(); f.interrupt();
    f.tcp(); f.interrupt(); f.tcp("1790000001200");
    assert(f.aborts==1 && f.p.interrupt_time_ms_==1790000001500);
    f.stream_end();
    f.event(TAI_EVT_END); f.nlg();
    f.stream=2;                       // B's stream, ts 2000 > cut-off
    f.audio();
    // B's stream (ts 2000) is newer than the cut-off, so its START cleared it.
    // The deduped copies then hit an idle session: flush nothing, arm nothing.
    f.stream_end();
    f.tcp(); f.interrupt();
    assert(f.aborts==1 && f.playback.size()==80 && f.p.interrupt_time_ms_==0);
    f.tcp("1790000002500");
    assert(f.aborts==2 && f.p.interrupt_time_ms_==1790000002500);
    // The cut-off ends only when the next stream's newer START arrives.
    f.stream_end();
    f.event(TAI_EVT_END); f.nlg();
    f.interrupt("1790000002500");
    assert(f.aborts==2 && f.p.interrupt_time_ms_==1790000002500);
    f.stream=3;
    f.audio();
    assert(f.p.interrupt_time_ms_==0 && f.playback.size()==80);
}
void callback_routing() {
    Fixture f;
    const std::string attr=R"({"breakAttributes":{"time":"1790000001500"}})";
    f.nlg(); f.audio();
    f.event(TAI_EVT_CHAT_BREAK, attr, "not attribute JSON");
    assert(f.aborts==1 && f.p.last_interrupt_time_=="1790000001500");
    assert(f.p.interrupt_time_ms_==1790000001500);
    // Filtering persists until a newer stream START; the payload bytes of a
    // CHAT_BREAK never feed audio parsing.
    f.stream_end();
    f.event(TAI_EVT_END); f.nlg();
    f.stream=2;
    f.audio();
    assert(f.p.interrupt_time_ms_==0);
    f.event(TAI_EVT_CHAT_BREAK, attr);
    assert(f.aborts==1 && f.playback.size()==80 && f.p.interrupt_time_ms_==0);
    // A timestamp in payload must not be used when attr 111 is absent. The
    // payload-free break is a plain CancelTurn: flush only, no cut-off, so
    // subsequent audio is unaffected.
    f.event(TAI_EVT_CHAT_BREAK, "", attr);
    assert(f.aborts==2 && f.p.interrupt_time_ms_==0);
    f.audio(80, 9);
    assert(f.playback.size()==80);

    int calls=0;
    f.p.on_incoming_json_=[&](const cJSON* root) {
        assert(strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(root, "type")), "mcp")==0);
        const cJSON* payload=cJSON_GetObjectItem(root, "payload");
        assert(strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(payload, "method")), "tools/list")==0);
        assert(cJSON_GetNumberValue(cJSON_GetObjectItem(payload, "id"))==7);
        ++calls;
    };
    const std::string mcp=R"({"jsonrpc":"2.0","id":7,"method":"tools/list"})";
    f.event(TAI_EVT_MCP_CMD, attr, mcp);
    f.event(TAI_EVT_MCP_CMD, "", mcp);
    assert(calls==2);
}
void no_start() {
    Fixture f;
    f.nlg();
    assert(f.p.response_receiving_);
    f.interrupt();
    assert(f.p.interrupt_time_ms_==1790000001500);
    f.event(TAI_EVT_END);
    // END does not clear the cut-off: A MIDDLE frame of the old stream
    // (ts <= cut-off) is still dropped, and END alone never reopens.
    f.stream_end();
    f.audio(13);
    assert(!f.p.response_receiving_ && f.playback.empty()
           && f.p.audio_reassembly_buf_.empty());
    f.interrupt("1790000002500");
    assert(f.p.interrupt_time_ms_==1790000002500 && f.p.audio_reassembly_buf_.empty());
    // END drops the interrupted stream's partial tail; the next response's
    // stream START (ts 3000 > cut-off) clears the cut-off and reopens.
    f.event(TAI_EVT_END); f.nlg();
    f.stream=3;
    f.audio();
    assert(f.playback.size()==80);
}
void ended_and_idle() {
    // Real device log: response A ended, response B already started streaming,
    // and A's delayed asrInterrupt arrives afterwards. Its timestamp
    // (1790000001500) sits below B's stream START timestamp (1790000002000),
    // so it flushes playback but must not drop any of B's audio or NLG.
    Fixture f;
    f.nlg(); f.audio(); f.stream_end(); f.event(TAI_EVT_END);
    assert(!f.p.response_receiving_ && f.playback.size()==80);
    f.event(TAI_EVT_START); f.nlg();
    f.stream=2;
    f.audio(160, 9);                        // B streaming (ts 1790000002000)
    assert(f.starts==2 && f.playback.size()==240);
    f.interrupt("1790000001500");           // A's break, older than B's START
    assert(f.aborts==1 && f.playback.empty());
    assert(f.p.interrupt_time_ms_==1790000001500);
    f.audio(160, 5);                        // B keeps streaming with MIDDLE flags
    assert(f.playback.size()==160);
    assert(std::vector<uint8_t>(f.playback.end()-160, f.playback.end())==std::vector<uint8_t>(160, 5));
    // A late frame of the ended stream A (ts <= cut-off) is still dropped.
    f.stream=1;
    f.audio(80, 1);
    assert(f.playback.size()==160);
    f.stream=2;
    f.stream_end();
    f.event(TAI_EVT_END);
    assert(f.stops==1);
}
void tcp_and_idle_vad() {
    Fixture f;
    f.event(TAI_EVT_SERVER_VAD);
    f.event(TAI_EVT_CHAT_BREAK);
    // The idle payload-free break flushed nothing and armed no cut-off.
    assert(!f.p.response_receiving_ && f.p.interrupt_time_ms_==0);
    // The response's NLG is still delivered (text is never discarded); its
    // audio plays too — an unstamped break filters nothing.
    f.nlg(); f.audio(23);
    assert(f.aborts==1 && f.starts==1 && f.sentences==1);
    assert(f.playback.empty());
    assert(f.p.response_receiving_ && f.p.audio_reassembly_buf_.size()==23);
    f.event(TAI_EVT_CHAT_BREAK);
    assert(f.aborts==2 && f.p.interrupt_time_ms_==0 && f.p.audio_reassembly_buf_.empty());
    // Payload-free TCP CHAT_BREAK always cancels (no duplicate special case);
    // the same stream's remaining audio still passes (cut-off stays 0).
    f.event(TAI_EVT_CHAT_BREAK); f.event(TAI_EVT_START); f.nlg();
    f.audio();
    assert(f.aborts==3 && f.starts==2 && f.playback.size()==80);
    f.stream_end();
    f.event(TAI_EVT_END); f.nlg();
    f.stream=2;
    f.audio();
    assert(f.starts==3 && f.playback.size()==160);
}
void delayed_cross_turn_break() {
    // Response A is interrupted by MQTT; A's delayed TCP CHAT_BREAK arrives
    // after B has already started and must not cancel or filter B.
    {
        // A's END arrives, then B's stream STARTs. A's delayed payload-free
        // TCP CHAT_BREAK still cancels once more, but flush-only: with no
        // cut-off armed, B's remaining MIDDLE audio keeps playing.
        Fixture f;
        f.nlg(); f.audio();                       // response A streaming
        f.interrupt("1790000001500");             // MQTT asrInterrupt for A
        assert(f.aborts==1 && f.p.interrupt_time_ms_==1790000001500 && f.playback.empty());
        f.stream_end();
        f.event(TAI_EVT_END);                     // A END: cut-off persists
        assert(f.p.interrupt_time_ms_==1790000001500 && !f.p.response_receiving_);
        f.event(TAI_EVT_START); f.nlg();
        f.stream=2;
        f.audio(80, 7);                           // B's stream STARTs (ts 2000)
        assert(f.p.interrupt_time_ms_==0);
        assert(f.starts==2 && f.playback==std::vector<uint8_t>(80, 7));
        f.event(TAI_EVT_CHAT_BREAK);              // A's delayed break, no attr 111
        assert(f.aborts==2 && f.p.interrupt_time_ms_==0);
        assert(f.p.response_receiving_);
        f.audio(80, 8);                           // B keeps streaming; flush stays
        assert(f.playback==std::vector<uint8_t>(80, 8));
        f.stream_end();
        f.event(TAI_EVT_END);
        // The second break suppressed B's first NLG, so its END emits no stop.
        assert(f.stops==0);
    }
    {
        // Delayed TCP CHAT_BREAK carrying a valid but older attr 111
        // timestamp: timestamp dedup must reject it, B survives.
        Fixture f;
        f.nlg(); f.audio();
        f.interrupt("1790000001500");             // MQTT break at t=...1500
        assert(f.aborts==1 && f.p.interrupt_time_ms_==1790000001500);
        f.stream_end();
        f.event(TAI_EVT_END);
        f.nlg();
        f.stream=2;
        f.audio(80, 7);                           // B streaming (ts 2000)
        f.tcp("1790000001200");                   // A's break, older t=...1200
        assert(f.aborts==1 && f.p.interrupt_time_ms_==0);
        assert(f.playback==std::vector<uint8_t>(80, 7));
        f.audio(80, 8);
        assert(f.playback.size()==160);
        f.event(TAI_EVT_END);
        assert(f.stops==1);
    }
    {
        // Same break moment delivered twice: MQTT then TCP attr 111 with an
        // equal timestamp. The equal-timestamp TCP copy must be deduped.
        Fixture f;
        f.nlg(); f.audio();
        f.interrupt("1790000001500");
        assert(f.aborts==1 && f.p.interrupt_time_ms_==1790000001500);
        f.tcp("1790000001500");                   // duplicate, same timestamp
        assert(f.aborts==1 && f.p.interrupt_time_ms_==1790000001500);
        f.stream_end();
        f.event(TAI_EVT_END);
        assert(f.p.interrupt_time_ms_==1790000001500);
        f.nlg();
        f.stream=2;  // B's stream START is newer, lifting the cut-off
        f.audio();
        assert(f.starts==2 && f.playback.size()==80);
    }
    {
        // END does not clear the cut-off: A's frames still arriving after its
        // END (ts at/below the interruption) are dropped until B's START.
        Fixture f;
        f.nlg(); f.audio();
        f.interrupt("1790000001500");
        f.stream_end();
        f.event(TAI_EVT_END);
        assert(f.p.interrupt_time_ms_==1790000001500 && !f.p.response_receiving_);
        f.audio(80, 3);                           // stale A frame after END
        assert(f.playback.empty());
        f.nlg();
        f.stream=2;
        f.audio();                                // B's START clears the cut-off
        assert(f.p.interrupt_time_ms_==0 && f.playback.size()==80);
    }
    {
        // A header-only START (len==0) newer than the cut-off clears it and
        // emits nothing; the stream's subsequent frames play.
        Fixture f;
        f.nlg(); f.audio();
        f.interrupt("1790000001500");
        assert(f.p.interrupt_time_ms_==1790000001500);
        f.stream_end();
        f.event(TAI_EVT_END); f.nlg();
        f.stream=2;
        f.header_start();
        assert(f.p.interrupt_time_ms_==0 && f.playback.empty());
        f.audio(80, 6);
        assert(f.playback==std::vector<uint8_t>(80, 6));
    }
}
void local_abort() {
    Fixture f;
    f.nlg(); f.audio(93);
    // Application flushes before calling SendAbortSpeaking; no second callback.
    f.playback.clear();
    sdk_hook=[&] {
        // An SDK call can wait on a receive callback: ctrl_mutex_ must be free.
        auto work=std::async(std::launch::async, [&] { f.p.ResetReceiveState(); });
        assert(work.wait_for(std::chrono::seconds(2))==std::future_status::ready);
    };
    f.p.SendAbortSpeaking(kAbortReasonNone);
    assert(f.aborts==0);
    sdk_hook=nullptr;
    f.nlg(); f.audio(13); f.p.SendAbortSpeaking(kAbortReasonNone);
    // Local abort is flush-only: it arms no cut-off (only an accepted
    // timestamped interruption does), so subsequent audio is unaffected.
    assert(f.p.interrupt_time_ms_==0 && f.p.audio_reassembly_buf_.empty());
    // SendStartListening only selects the mode; it touches no receive state.
    f.p.SendStartListening(kListeningModeRealtime);
    f.nlg();
    f.stream=2;
    f.audio(); assert(f.playback.size()==80);
    f.stream_end();
    f.event(TAI_EVT_END); f.nlg(); f.audio();
    // Local abort while idle: playback was already drained; only the next
    // stream's END is pending.
    f.playback.clear(); f.p.SendAbortSpeaking(kAbortReasonNone);
    assert(f.p.interrupt_time_ms_==0 && f.aborts==0);
    f.nlg();
    f.stream=3;
    f.audio();
    assert(f.playback.size()==80);
}
void malformed() {
    const std::vector<std::string> invalid={
        "", "{", "null", "[]", "{}", R"({"time":1})", R"({"time":null})",
        R"({"time":true})", R"({"time":[]})", R"({"time":""})",
        R"({"time":"17900000015001234"})", R"({"time":" 1790000001500"})",
        R"({"time":"-1790000001500"})", R"({"time":"179000000150x"})",
        R"({"time":"1790000001500evil"})", R"({"time":"1790000001500"}junk)",
        R"({"time":"1790000001500"})"+std::string(1, '\0')+"junk"
    };
    for (const auto& json: invalid) {
        Fixture f; f.nlg(); f.audio(13);
        f.mqtt(json);
        f.event(TAI_EVT_CHAT_BREAK, "{\"breakAttributes\":"+json+"}");
        assert(f.aborts==0 && f.p.last_interrupt_time_.empty());
        assert(f.p.interrupt_time_ms_==0 && f.p.audio_reassembly_buf_.size()==13);
    }
    Fixture f; f.nlg();
    std::string json=R"({ "time" : "1790000001500" } )";
    f.p.HandleAiControl("wrong", json.data(), json.size());
    f.p.HandleAiControl(nullptr, json.data(), json.size());
    f.p.HandleAiControl("asrInterrupt", nullptr, 100);
    f.p.HandleAiControl("asrInterrupt", json.data(), json.size()-5);
    assert(f.aborts==0);
    f.mqtt(json); assert(f.aborts==1);
    f.event(TAI_EVT_END); f.nlg();
    f.mqtt(R"({"time":"1790000002500"})");
    assert(f.aborts==2 && f.p.last_interrupt_time_=="1790000002500");
    f.event(TAI_EVT_END); f.nlg();
    json=R"({"time":"1790000003500"})";
    f.p.HandleAiControl("asrInterrupt", json.c_str(), json.size()+1);
    assert(f.aborts==3 && f.p.interrupt_time_ms_==1790000003500);
}
void mode() {
    Fixture f;
    f.p.SendStartListening(kListeningModeAutoStop);
    f.nlg(); f.interrupt(); f.tcp();
    assert(f.aborts==0);
    f.event(TAI_EVT_END); assert(f.stream_ends==1 && f.stops==0);
    f.p.SendStartListening(kListeningModeRealtime);
    f.nlg(); f.interrupt(); assert(f.aborts==1);
    f.p.SendStartListening(kListeningModeManualStop);
    assert(f.p.interrupt_time_ms_==1790000001500);
    f.event(TAI_EVT_END); f.nlg(); f.tcp("1790000002500");
    assert(f.aborts==1);
}
void reset() {
    Fixture f; f.nlg(); f.interrupt();
    f.p.HandleDisconnect(0, 0, 0, 0);
    assert(f.p.interrupt_time_ms_==0 && !f.p.response_receiving_ && !f.p.realtime_mode_);
    assert(f.p.last_interrupt_time_.empty() && f.p.audio_reassembly_buf_.empty());
    f.interrupt(); assert(f.aborts==1);
    auto tasks=std::move(Application::GetInstance().tasks);
    // Close must permit receive callbacks while tai_disconnect joins.
    sdk_hook=[&] {
        auto worker=std::async(std::launch::async, [&] { f.event(TAI_EVT_END); });
        assert(worker.wait_for(std::chrono::seconds(2))==std::future_status::ready);
    };
    for (auto& task: tasks) task();
    f.p.response_receiving_=true; f.p.interrupt_time_ms_=9999999999999;
    f.p.last_interrupt_time_="9999999999999";
    sdk_hook=[&] {
        assert(!f.p.response_receiving_ && f.p.interrupt_time_ms_==0 && f.p.last_interrupt_time_.empty());
        auto worker=std::async(std::launch::async, [&] { f.nlg(); f.audio(13); });
        assert(worker.wait_for(std::chrono::seconds(2))==std::future_status::ready);
    };
    assert(f.p.OpenAudioChannel());
    // An early callback from tai_connect must not be erased after it returns.
    assert(f.p.response_receiving_ && f.p.has_received_first_nlg_ && f.p.audio_reassembly_buf_.size()==13);
    sdk_hook=nullptr;
    f.p.SendStartListening(kListeningModeRealtime);
    assert(f.p.has_received_first_nlg_ && f.p.audio_reassembly_buf_.size()==13);
    f.interrupt(); assert(f.aborts==2);
    f.p.CloseAudioChannel(false);
    assert(!f.p.response_receiving_ && f.p.interrupt_time_ms_==0 && f.p.last_interrupt_time_.empty());
}
void concurrent() {
    Fixture f;
    std::mutex mutex;
    std::condition_variable cv;
    bool entered=false, release=false;
    f.p.on_incoming_audio_=[&](std::unique_ptr<AudioStreamPacket> packet) {
        std::unique_lock<std::mutex> lock(mutex);
        entered=true; cv.notify_all();
        cv.wait(lock, [&] { return release; });
        f.playback.insert(f.playback.end(), packet->payload.begin(), packet->payload.end());
    };
    auto audio=std::async(std::launch::async, [&] { f.audio(); });
    {
        std::unique_lock<std::mutex> lock(mutex);
        assert(cv.wait_for(lock, std::chrono::seconds(2), [&] { return entered; }));
    }
    std::promise<void> attempting;
    auto mqtt=std::async(std::launch::async, [&] { attempting.set_value(); f.interrupt(); });
    attempting.get_future().wait();
    // Cancellation cannot pass an in-flight enqueue and then be refilled by it.
    assert(mqtt.wait_for(std::chrono::milliseconds(50))==std::future_status::timeout);
    {
        std::lock_guard<std::mutex> lock(mutex); release=true;
    }
    cv.notify_all(); audio.get(); mqtt.get();
    assert(f.aborts==1 && f.playback.empty() && f.p.interrupt_time_ms_==1790000001500);
    // A MIDDLE continuation of the interrupted stream (ts <= cut-off) is
    // dropped.
    f.audio(); assert(f.playback.empty() && f.starts==0);
    // The next turn's NLG is never discarded; its stream START is newer than
    // the cut-off, so the turn's audio plays.
    f.nlg();
    f.stream=2;
    f.audio();
    assert(f.starts==1 && f.sentences==1 && f.playback.size()==80);
}
void c3() {
    Fixture f;
    f.nlg(); f.audio(13); f.interrupt();
    assert(f.p.realtime_mode_ && f.aborts==0 && f.p.interrupt_time_ms_==0);
    assert(f.p.last_interrupt_time_.empty() && f.p.audio_reassembly_buf_.size()==13);
    f.p.SendAbortSpeaking(kAbortReasonNone);
    // Local abort is flush-only: no cut-off is armed.
    assert(f.aborts==0 && f.p.interrupt_time_ms_==0 && f.p.audio_reassembly_buf_.empty());
    f.event(TAI_EVT_START); f.nlg();
    f.stream=2;
    f.audio(); assert(f.playback.size()==80);
    f.stream_end();
    f.event(TAI_EVT_END); f.nlg(); f.audio(); assert(f.playback.size()==160);
}
void mqtt_connect() {
    Fixture f;
    mqtt_calls.clear(); assert(f.p.ConnectMqtt());
#if CONFIG_IDF_TARGET_ESP32C3
    assert((mqtt_calls==std::vector<std::string>{"connect", "pump"}));
#else
    assert((mqtt_calls==std::vector<std::string>{"register", "connect", "pump"}));
#endif
}
int main(int argc, char** argv) {
    assert(argc==2);
    const std::string name=argv[1];
    if (name=="receiving") receiving();
    else if (name=="dedup") dedup();
    else if (name=="callback_routing") callback_routing();
    else if (name=="no_start") no_start();
    else if (name=="ended_and_idle") ended_and_idle();
    else if (name=="delayed_cross_turn_break") delayed_cross_turn_break();
    else if (name=="local_abort") local_abort();
    else if (name=="tcp_and_idle_vad") tcp_and_idle_vad();
    else if (name=="malformed") malformed();
    else if (name=="mode") mode();
    else if (name=="reset") reset();
    else if (name=="concurrent") concurrent();
    else if (name=="c3") c3();
    else if (name=="mqtt_connect") mqtt_connect();
    else assert(false);
}
'''


class TuyaInterruptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.path = Path(cls.temp.name)
        source = (ROOT / 'main/protocols/tuya_protocol.cc').read_text()
        methods = '\n'.join(method(source, 'TuyaProtocol::' + name + '(') for name in (
            'HandleAiControl', 'AcceptInterruptTime', 'HandleEvent', 'HandleText',
            'HandleAudio', 'CancelTurn', 'ResetReceiveState', 'OnFlowControlCb',
            'SendAbortSpeaking', 'SendStartListening', 'HandleDisconnect',
            'OpenAudioChannel', 'CloseAudioChannel', 'ConnectMqtt', 'OnAiCtrlCb',
            'OnEventCb'))
        (cls.path / 'test.cc').write_text(HARNESS.replace('// METHODS', methods))
        subprocess.run(['cc', '-O1', '-g', '-fsanitize=address,undefined',
                        '-c', str(CJSON / 'cJSON.c'), '-o', str(cls.path / 'cjson.o')],
                       check=True, timeout=60)
        for target, flags in (('s3', ['-DCONFIG_IDF_TARGET_ESP32S3=1']),
                              ('c3', ['-DCONFIG_IDF_TARGET_ESP32C3=1',
                                      '-DCONFIG_USE_SERVER_AEC=1'])):
            subprocess.run(['c++', '-std=c++17', '-pthread', '-O1', '-g',
                            '-fsanitize=address,undefined', *flags, '-I', str(CJSON),
                            '-I', str(SDK / 'modules/rtc-tcp-client/include'),
                            '-I', str(SDK / 'pal'), '-I', str(SDK / 'common'),
                            str(cls.path / 'test.cc'), str(cls.path / 'cjson.o'),
                            '-o', str(cls.path / target)], check=True, timeout=60)

    def run_scenario(self, name, target='s3'):
        subprocess.run([str(self.path / target), name], check=True, timeout=15)

    def test_receive_discard_until_stream_start_reopens_flow_control(self):
        self.run_scenario('receiving')

    def test_mqtt_tcp_timestamp_dedup_across_responses(self):
        self.run_scenario('dedup')

    def test_callback_routes_break_attributes_and_preserves_mcp_payload(self):
        for target in ('s3', 'c3'):
            self.run_scenario('callback_routing', target)

    def test_nlg_and_partial_audio_establish_response_without_start(self):
        self.run_scenario('no_start')

    def test_end_already_received_and_idle_do_not_discard_next_response(self):
        self.run_scenario('ended_and_idle')

    def test_payload_free_tcp_break_and_idle_vad(self):
        self.run_scenario('tcp_and_idle_vad')

    def test_delayed_cross_turn_tcp_break_does_not_cancel_next_response(self):
        self.run_scenario('delayed_cross_turn_break')

    def test_local_abort_suppresses_without_duplicate_callback_or_sdk_lock(self):
        self.run_scenario('local_abort')

    def test_length_aware_json_and_invalid_timestamps(self):
        self.run_scenario('malformed')

    def test_listening_mode_updates_without_receive_reset(self):
        self.run_scenario('mode')

    def test_disconnect_close_and_open_reset_order(self):
        self.run_scenario('reset')

    def test_audio_enqueue_serializes_with_mqtt_flush(self):
        self.run_scenario('concurrent')

    def test_c3_ignores_mqtt_even_with_server_aec(self):
        self.run_scenario('c3', 'c3')

    def test_mqtt_registration_and_restart_helper(self):
        for target in ('s3', 'c3'):
            self.run_scenario('mqtt_connect', target)
        source = (ROOT / 'main/protocols/tuya_protocol.cc').read_text()
        for name in ('Start', 'RefreshTaiContext'):
            self.assertIn('if (!ConnectMqtt()) return false;',
                          method(source, 'TuyaProtocol::' + name + '('))
