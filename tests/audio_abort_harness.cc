#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <future>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>
using namespace std::chrono_literals;
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define CONFIG_USE_SERVER_AEC 1
constexpr int MAX_PLAYBACK_TASKS_IN_QUEUE=2, MAX_SEND_PACKETS_IN_QUEUE=60;
constexpr int MAX_DECODE_PACKETS_IN_QUEUE=2, OPUS_FRAME_DURATION_MS=40;
constexpr int AUDIO_POWER_CHECK_INTERVAL_MS=1000;
constexpr int ESP_AUDIO_ERR_OK=0, ESP_AUDIO_ERR_BUFF_NOT_ENOUGH=1, ESP_AUDIO_DEC_RECOVERY_NONE=0;
std::function<void()> resample_hook, enable_hook, packet_destroy_hook, output_hook;
std::promise<void>* full_queue = nullptr;
// A real condition_variable with a deterministic signal when a producer waits.
struct TestCV : std::condition_variable {
    template<class Lock, class Predicate> void wait(Lock& lock, Predicate predicate) {
        if (full_queue && !predicate()) { full_queue->set_value(); full_queue=nullptr; }
        std::condition_variable::wait(lock, predicate);
    }
};
struct AudioStreamPacket { int sample_rate=16000, frame_duration=40; uint32_t timestamp=1; std::vector<uint8_t> payload; };
struct DecodeAudioPacket : AudioStreamPacket { ~DecodeAudioPacket() { if(packet_destroy_hook) packet_destroy_hook(); } };
enum AudioTaskType { kAudioTaskTypeDecodeToPlaybackQueue, kAudioTaskTypeEncodeToSendQueue, kAudioTaskTypeEncodeToTestingQueue };
struct AudioTask { AudioTaskType type; std::vector<int16_t> pcm; uint32_t timestamp=1; };
struct Codec {
    bool enabled=false;
    std::atomic<int> outputs{0};
    bool output_enabled() { return enabled; }
    void EnableOutput(bool value) { if(enable_hook) enable_hook(); enabled=value; }
    void OutputData(std::vector<int16_t>&) { ++outputs; if(output_hook) output_hook(); }
    int output_sample_rate() { return 48000; }
};
void esp_timer_stop(int) {}
void esp_timer_start_periodic(int, int) {}
void esp_opus_dec_reset(void*) {}
struct esp_audio_dec_in_raw_t { uint8_t* buffer; uint32_t len,consumed; int frame_recover; };
struct esp_audio_dec_out_frame_t { uint8_t* buffer; uint32_t len,decoded_size,needed_size=0; };
struct esp_audio_dec_info_t {};
int esp_opus_dec_decode(void*, esp_audio_dec_in_raw_t*, esp_audio_dec_out_frame_t* out, esp_audio_dec_info_t*) { out->decoded_size=2; return 0; }
using esp_ae_sample_t = void*;
void esp_ae_rate_cvt_get_max_out_sample_num(void*, size_t, uint32_t* count) { *count=1; }
void esp_ae_rate_cvt_process(void*, void*, size_t, void*, uint32_t*) { if(resample_hook) resample_hook(); }
struct esp_audio_enc_in_frame_t { uint8_t* buffer; uint32_t len; };
struct esp_audio_enc_out_frame_t { uint8_t* buffer; uint32_t len,encoded_bytes; };
int esp_opus_enc_process(void*, esp_audio_enc_in_frame_t*, esp_audio_enc_out_frame_t*) { return -1; }
struct AudioService {
    Codec codec;
    Codec* codec_=&codec;
    std::mutex audio_queue_mutex_, decoder_mutex_;
    TestCV audio_queue_cv_;
    bool service_stopped_=false;
    std::atomic<bool> output_aborted_{false};
    std::atomic<uint32_t> output_generation_{0};
    std::deque<std::unique_ptr<DecodeAudioPacket>> audio_decode_queue_;
    std::deque<std::unique_ptr<AudioTask>> audio_playback_queue_,audio_encode_queue_;
    std::deque<std::unique_ptr<AudioStreamPacket>> audio_testing_queue_,audio_send_queue_;
    std::deque<uint32_t> timestamp_queue_;
    void *opus_decoder_=(void*)1, *opus_encoder_=nullptr, *output_resampler_=(void*)1;
    int decoder_frame_size_=1, decoder_sample_rate_=16000, encoder_frame_size_=1, encoder_outbuf_size_=1;
    int audio_power_timer_=0;
    std::chrono::steady_clock::time_point last_output_time_;
    struct { int playback_count=0,decode_count=0,encode_count=0; } debug_statistics_;
    struct { std::function<void()> on_send_queue_available; } callbacks_;
    void SetDecodeSampleRate(int,int) {}
    void AbortOutput(); void ResetDecoder(); void FlushAudioQueues();
    bool PushPacketToDecodeQueue(std::unique_ptr<AudioStreamPacket>, bool);
    void AudioOutputTask(); void OpusCodecTask();
    void stop() { std::lock_guard<std::mutex> lock(audio_queue_mutex_); service_stopped_=true; audio_queue_cv_.notify_all(); }
};
// AUDIO_SERVICE_METHODS
int main() {
    // Root regression: AbortOutput must not clear owning queues while another
    // thread has their lock. Fails deterministically against the unlocked version.
    {
        AudioService service;
        service.audio_decode_queue_.push_back(std::make_unique<DecodeAudioPacket>());
        std::unique_lock<std::mutex> lock(service.audio_queue_mutex_);
        std::promise<void> started;
        auto result=std::async(std::launch::async,[&]{started.set_value();service.AbortOutput();});
        started.get_future().wait();
        assert(result.wait_for(100ms)==std::future_status::timeout);
        lock.unlock(); result.get();
        assert(service.audio_decode_queue_.empty());
    }
    // Old decode finishes after abort AND reset have cleared the boolean flag.
    // It must not repopulate the new playback queue. Repeat for a normal flush.
    for (bool flush : {false,true}) {
        AudioService service;
        std::promise<void> entered,release,destroyed;
        auto ready=release.get_future();
        resample_hook=[&]{entered.set_value();ready.wait();};
        packet_destroy_hook=[&]{destroyed.set_value();};
        service.audio_decode_queue_.push_back(std::make_unique<DecodeAudioPacket>());
        std::thread worker([&]{service.OpusCodecTask();});
        entered.get_future().wait();
        if(flush) service.FlushAudioQueues(); else {service.AbortOutput();service.ResetDecoder();}
        release.set_value(); destroyed.get_future().wait();
        service.stop();worker.join();
        assert(service.audio_playback_queue_.empty());
        resample_hook={};packet_destroy_hook={};
    }
    // A popped PCM frame paused while enabling output must remain invalid after
    // ResetDecoder re-enables the next turn.
    {
        AudioService service;
        std::promise<void> entered,release;
        auto ready=release.get_future();
        enable_hook=[&]{entered.set_value();ready.wait();};
        service.audio_playback_queue_.push_back(std::make_unique<AudioTask>());
        std::thread worker([&]{service.AudioOutputTask();});
        entered.get_future().wait();service.AbortOutput();service.ResetDecoder();
        service.stop();release.set_value();worker.join();
        assert(service.codec.outputs==0);enable_hook={};
    }
    // Producer blocked on a full queue belongs to the invalidated generation.
    {
        AudioService service;
        for(int i=0;i<MAX_DECODE_PACKETS_IN_QUEUE;++i) service.audio_decode_queue_.push_back(std::make_unique<DecodeAudioPacket>());
        std::promise<void> waiting;full_queue=&waiting;
        auto producer=std::async(std::launch::async,[&]{return service.PushPacketToDecodeQueue(std::make_unique<AudioStreamPacket>(),true);});
        waiting.get_future().wait();service.AbortOutput();service.ResetDecoder();
        assert(!producer.get());assert(service.audio_decode_queue_.empty());
    }
    // A subsequent generation must still play normally after abort/reset.
    {
        AudioService service;
        service.AbortOutput(); service.ResetDecoder();
        std::promise<void> played;
        output_hook=[&]{played.set_value();};
        service.audio_playback_queue_.push_back(std::make_unique<AudioTask>());
        std::thread worker([&]{service.AudioOutputTask();});
        played.get_future().wait();service.stop();worker.join();
        assert(service.codec.outputs==1);output_hook={};
    }

}
