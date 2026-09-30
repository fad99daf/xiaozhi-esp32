"""Actual AudioService initialization/enable/prompt methods with host SDK stubs."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_audio_abort import method

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
#include <atomic>
#include <cassert>
#include <condition_variable>
#include <cstring>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGD(...) ((void)0)
constexpr int MALLOC_CAP_INTERNAL = 1, MALLOC_CAP_8BIT = 2, ESP_TIMER_TASK = 0;
constexpr int ESP_AUDIO_SAMPLE_RATE_16K = 16000, ESP_AUDIO_MONO = 1, ESP_AUDIO_BIT16 = 16;
constexpr int ESP_OPUS_ENC_APPLICATION_VOIP = 1;
using esp_opus_enc_frame_duration_t = int;
constexpr int ESP_OPUS_ENC_FRAME_DURATION_5_MS = 5, ESP_OPUS_ENC_FRAME_DURATION_10_MS = 10,
    ESP_OPUS_ENC_FRAME_DURATION_20_MS = 20, ESP_OPUS_ENC_FRAME_DURATION_40_MS = 40,
    ESP_OPUS_ENC_FRAME_DURATION_60_MS = 60, ESP_OPUS_ENC_FRAME_DURATION_80_MS = 80,
    ESP_OPUS_ENC_FRAME_DURATION_100_MS = 100, ESP_OPUS_ENC_FRAME_DURATION_120_MS = 120;
// PRODUCTION_CONFIG
struct esp_opus_enc_config_t {
    int sample_rate, channel, bits_per_sample, bitrate, frame_duration, application_mode, complexity;
    bool enable_fec, enable_dtx, enable_vbr;
};
struct esp_opus_dec_cfg_t { int sample_rate, frame_duration; };
#define OPUS_DEC_CFG(rate, duration) {rate, duration}
struct esp_ae_rate_cvt_cfg_t { int rate, target, channels; };
#define RATE_CVT_CFG(rate, target, channels) {rate, target, channels}
size_t heap_caps_get_free_size(int) { return 100000; }
void LogAudioHeap(const char*, size_t&) {}
std::vector<std::string> events;
int opens = 0, frame_queries = 0;
bool fail_open = false;
std::mutex* queue_mutex = nullptr;
int encoder_handle, decoder_handle;
int esp_opus_enc_open(esp_opus_enc_config_t* cfg, size_t size, void** encoder) {
    // A different thread must not acquire the queue lock during publication.
    bool acquired = false;
    std::thread probe([&] {
        acquired = queue_mutex->try_lock();
        if (acquired) queue_mutex->unlock();
    });
    probe.join();
    assert(!acquired);
    ++opens;
    events.push_back("encoder.open");
    assert(size == sizeof(*cfg));
    assert(cfg->sample_rate == 16000 && cfg->channel == 1 && cfg->bits_per_sample == 16);
    assert(cfg->bitrate == 16000 && cfg->frame_duration == 40 && cfg->complexity == 0);
    assert(cfg->application_mode == ESP_OPUS_ENC_APPLICATION_VOIP);
    assert(!cfg->enable_fec && !cfg->enable_dtx && !cfg->enable_vbr);
    *encoder = fail_open ? nullptr : &encoder_handle;
    return fail_open ? -1 : 0;
}
int esp_opus_enc_get_frame_size(void* encoder, int* input, int* output) {
    assert(encoder == &encoder_handle);
    ++frame_queries;
    *input = 1280;
    *output = 321;
    events.push_back("encoder.sized");
    return 0;
}
int esp_opus_dec_open(esp_opus_dec_cfg_t*, size_t, void** decoder) {
    *decoder = &decoder_handle;
    return 0;
}
int esp_ae_rate_cvt_open(esp_ae_rate_cvt_cfg_t*, void**) { return 0; }
void esp_ae_rate_cvt_reset(void*) {}
struct esp_timer_create_args_t {
    void (*callback)(void*);
    void* arg;
    int dispatch_method;
    const char* name;
    bool skip_unhandled_events;
};
void esp_timer_create(esp_timer_create_args_t*, void**) {}
void esp_timer_stop(void*) {}
void esp_timer_start_periodic(void*, int) {}
void xEventGroupSetBits(int* bits, int value) {
    events.push_back("bits.set"); *bits |= value;
}
void xEventGroupClearBits(int* bits, int value) { *bits &= ~value; }
struct AudioCodec {
    bool output = false;
    void Start() { events.push_back("codec.start"); }
    int output_sample_rate() { return 16000; }
    int input_sample_rate() { return 16000; }
    int input_channels() { return 1; }
    bool output_enabled() { return output; }
    void EnableOutput(bool enable) { output = enable; }
};
struct NoAudioProcessor {
    void OnOutput(std::function<void(std::vector<int16_t>&&)>) {}
    void OnVadStateChange(std::function<void(bool)>) {}
    void Initialize(AudioCodec*, int, void*) { events.push_back("processor.initialize"); }
    void Start() { events.push_back("processor.start"); }
    void Stop() {}
};
using AfeAudioProcessor = NoAudioProcessor;
struct AudioTask {
    int type;
    std::vector<int16_t> pcm;
    uint32_t timestamp = 0;
};
constexpr int kAudioTaskTypeEncodeToSendQueue = 0;
struct AudioStreamPacket {
    int sample_rate = 0, frame_duration = 0;
    uint32_t timestamp = 0;
    std::vector<uint8_t> payload;
};
using DecodeAudioPacket = AudioStreamPacket;
struct OggDemuxer {
    std::function<void(const uint8_t*, int, size_t)> output;
    void OnDemuxerFinished(decltype(output) callback) { output = callback; }
    void Reset() {}
    void Process(const uint8_t* data, size_t size) { output(data, 16000, size); }
};
struct AudioService {
    AudioCodec* codec_ = nullptr;
    std::unique_ptr<NoAudioProcessor> audio_processor_;
    void *opus_encoder_ = nullptr, *opus_decoder_ = nullptr, *input_resampler_ = nullptr;
    void *models_list_ = nullptr, *audio_power_timer_ = nullptr;
    int encoder_sample_rate_ = 0, encoder_duration_ms_ = 0;
    int encoder_frame_size_ = 0, encoder_outbuf_size_ = 0;
    int decoder_sample_rate_ = 0, decoder_duration_ms_ = 0, decoder_frame_size_ = 0;
    bool voice_detected_ = false, audio_processor_initialized_ = false;
    bool audio_input_need_warmup_ = false, service_stopped_ = false;
    std::atomic<uint32_t> output_generation_{0};
    int bits = 0;
    int* event_group_ = &bits;
    std::mutex audio_queue_mutex_, input_resampler_mutex_;
    std::condition_variable audio_queue_cv_;
    std::deque<std::unique_ptr<AudioTask>> audio_encode_queue_;
    std::deque<std::unique_ptr<AudioStreamPacket>> audio_send_queue_, audio_testing_queue_;
    std::deque<std::unique_ptr<DecodeAudioPacket>> audio_decode_queue_;
    std::deque<uint32_t> timestamp_queue_;
    struct { std::function<void(bool)> on_vad_change; } callbacks_;
    void CheckAndUpdateAudioPowerState() {}
    void ResetDecoder() { events.push_back("decoder.reset"); }
    void Initialize(AudioCodec*);
    bool EnsureEncoderInitialized();
    void EnableVoiceProcessing(bool);
    void EnableAudioTesting(bool);
    void PlaySound(const std::string_view&);
    bool PushPacketToDecodeQueue(std::unique_ptr<AudioStreamPacket>, bool);
};
// PRODUCTION_METHODS
void assert_ready(const AudioService& audio) {
    assert(audio.opus_encoder_ == &encoder_handle);
    assert(audio.encoder_sample_rate_ == 16000 && audio.encoder_duration_ms_ == 40);
    assert(audio.encoder_frame_size_ == 640 && audio.encoder_outbuf_size_ == 321);
}
int main(int argc, char** argv) {
    assert(argc == 2);
    const std::string scenario = argv[1];
    AudioService audio;
    AudioCodec codec;
    queue_mutex = &audio.audio_queue_mutex_;
    audio.Initialize(&codec);
#if CONFIG_IDF_TARGET_ESP32C3 && CONFIG_TUYA_BLE_PROVISIONING
    constexpr bool lazy = true;
#else
    constexpr bool lazy = false;
#endif
    assert(opens == (lazy ? 0 : 1));
    assert(audio.opus_decoder_ != nullptr && audio.audio_processor_ != nullptr);
    if (!lazy) assert_ready(audio);
    events.clear();
    if (scenario == "disable") {
        audio.EnableVoiceProcessing(false);
        audio.EnableAudioTesting(false);
        assert(opens == (lazy ? 0 : 1) && audio.bits == 0);
    } else if (scenario == "prompt") {
        audio.PlaySound("stub opus packet");
        assert(codec.output && audio.audio_decode_queue_.size() == 1);
        assert(audio.audio_decode_queue_.front()->payload.size() == 16);
        assert(opens == (lazy ? 0 : 1));
    } else if (scenario == "concurrent") {
        std::vector<std::thread> threads;
        for (int i = 0; i < 8; ++i)
            threads.emplace_back([&] { assert(audio.EnsureEncoderInitialized()); });
        for (auto& thread : threads) thread.join();
        assert_ready(audio);
        assert(opens == 1 && frame_queries == 1);
    } else {
        const bool voice = scenario.find("voice") != std::string::npos;
        const bool failure = scenario.find("failure") != std::string::npos;
        auto enable = [&](bool value) {
            if (voice) audio.EnableVoiceProcessing(value);
            else audio.EnableAudioTesting(value);
        };
        const int bit = voice ? AS_EVENT_AUDIO_PROCESSOR_RUNNING : AS_EVENT_AUDIO_TESTING_RUNNING;
        if (failure && lazy) {
            fail_open = true;
            enable(true);
            assert(events == std::vector<std::string>{"encoder.open"});
            assert(audio.bits == 0 && !audio.audio_processor_initialized_);
            assert(!audio.audio_input_need_warmup_ && audio.opus_encoder_ == nullptr);
            assert(audio.encoder_frame_size_ == 0 && audio.encoder_outbuf_size_ == 0);
            assert(frame_queries == 0);
            enable(false);
            assert(opens == 1);
            fail_open = false;
            events.clear();
        }
        enable(true);
        std::vector<std::string> expected;
        if (lazy) expected = {"encoder.open", "encoder.sized"};
        if (voice) expected.insert(expected.end(), {
            "processor.initialize", "decoder.reset", "processor.start"});
        expected.push_back("bits.set");
        assert(events == expected);
        assert_ready(audio);
        assert(audio.bits == bit);
        void* retained = audio.opus_encoder_;
        enable(false);
        assert(audio.bits == 0 && audio.opus_encoder_ == retained);
        enable(true);
        // The other capture path must reuse the same encoder too.
        if (voice) audio.EnableAudioTesting(true);
        else audio.EnableVoiceProcessing(true);
        assert(opens == (failure && lazy ? 2 : 1) && frame_queries == 1);
        assert(audio.opus_encoder_ == retained);
    }
}
'''


class AudioEncoderInitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (ROOT / 'main/audio/audio_service.cc').read_text()
        header = (ROOT / 'main/audio/audio_service.h').read_text()
        config = header[header.index('#define OPUS_FRAME_DURATION_MS'):
                        header.index('struct AudioServiceCallbacks')]
        if 'bool AudioService::EnsureEncoderInitialized(' not in source:
            raise AssertionError('AudioService::EnsureEncoderInitialized is not implemented yet')
        methods = '\n'.join(method(source, 'AudioService::' + name + '(') for name in (
            'Initialize', 'EnsureEncoderInitialized', 'EnableVoiceProcessing',
            'EnableAudioTesting', 'PlaySound', 'PushPacketToDecodeQueue'))
        cls.temp = tempfile.TemporaryDirectory(dir='/tmp')
        cls.addClassCleanup(cls.temp.cleanup)
        path = Path(cls.temp.name)
        cpp = path / 'encoder.cc'
        cpp.write_text(HARNESS.replace('// PRODUCTION_CONFIG', config)
                       .replace('// PRODUCTION_METHODS', methods))
        cls.executables = {}
        for chip in ('ESP32C3', 'ESP32S3'):
            for ble in (0, 1):
                name = f'{chip}_ble{ble}'
                executable = path / name
                subprocess.run(['c++', '-std=c++17', '-pthread', '-O1', '-g',
                                '-fsanitize=address,undefined',
                                f'-DCONFIG_IDF_TARGET_{chip}=1',
                                f'-DCONFIG_TUYA_BLE_PROVISIONING={ble}',
                                str(cpp), '-o', str(executable)], check=True, timeout=60)
                cls.executables[name] = executable

    def run_scenario(self, scenario):
        for name, executable in self.executables.items():
            with self.subTest(configuration=name):
                result = subprocess.run([str(executable), scenario], capture_output=True,
                                        text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_disable_never_opens_encoder(self):
        self.run_scenario('disable')

    def test_prompt_queues_audio_without_opening_encoder(self):
        self.run_scenario('prompt')

    def test_concurrent_initialization_opens_once(self):
        self.run_scenario('concurrent')

    def test_voice_enable_orders_initialization_and_retains_encoder(self):
        self.run_scenario('voice')

    def test_testing_enable_orders_initialization_and_retains_encoder(self):
        self.run_scenario('testing')

    def test_voice_open_failure_does_not_enable_and_can_retry(self):
        self.run_scenario('voice_failure')

    def test_testing_open_failure_does_not_enable_and_can_retry(self):
        self.run_scenario('testing_failure')


if __name__ == '__main__':
    unittest.main()
