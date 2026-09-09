"""Exercise actual Tuya audio reassembly with fragmented and bursty input."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from test_audio_abort import method

ROOT = Path(__file__).resolve().parents[1]


class TuyaAudioMemoryTests(unittest.TestCase):
    def test_bursts_and_partial_frames_use_bounded_scratch(self):
        source = (ROOT / 'main/protocols/tuya_protocol.cc').read_text()
        body = method(source, 'TuyaProtocol::HandleAudio(')
        harness = r'''
#include <cassert>
#include <cstdint>
#include <functional>
#include <memory>
#include <vector>
#define ESP_LOGI(...) ((void)0)
constexpr int TAI_OPUS_FRAME_SIZE_BYTES = 80, TAI_OPUS_FRAME_DURATION_MS = 40;
struct AudioStreamPacket {
    std::vector<uint8_t> payload;
    int sample_rate, frame_duration;
};
struct TuyaProtocol {
    int audio_recv_count_ = 0, turn_count_ = 0;
    bool first_tts_audio_pending_ = true;
    int server_sample_rate_ = 16000, server_frame_duration_ = 40;
    std::vector<uint8_t> audio_reassembly_buf_;
    std::function<void(std::unique_ptr<AudioStreamPacket>)> on_incoming_audio_;
    void HandleAudio(const uint8_t*, size_t, uint32_t, uint16_t);
};
// METHOD
int main() {
    // Exercise every possible partial-frame boundary followed by a large burst.
    for (size_t split = 1; split < 80; ++split) {
        TuyaProtocol protocol;
        std::vector<uint8_t> input(80 * 150 + 17), actual;
        for (size_t i = 0; i < input.size(); ++i) input[i] = i % 251;
        protocol.on_incoming_audio_ = [&](std::unique_ptr<AudioStreamPacket> p) {
            assert(p->payload.size() == 80);
            assert(p->sample_rate == 16000 && p->frame_duration == 40);
            actual.insert(actual.end(), p->payload.begin(), p->payload.end());
        };
        protocol.HandleAudio(input.data(), split, 16000, 40);
        assert(actual.empty());
        protocol.HandleAudio(input.data()+split, input.size()-split, 0, 0);
        assert(actual == std::vector<uint8_t>(input.begin(), input.end()-17));
        assert(protocol.audio_reassembly_buf_ == std::vector<uint8_t>(input.end()-17, input.end()));
        // std::vector may grow geometrically, but must not retain a network burst.
        assert(protocol.audio_reassembly_buf_.capacity() <= 160);
        std::vector<uint8_t> rest(63, 42);
        protocol.HandleAudio(rest.data(), rest.size(), 0, 0);
        input.insert(input.end(), rest.begin(), rest.end());
        assert(actual == input);
        assert(protocol.audio_reassembly_buf_.empty());
        protocol.HandleAudio(nullptr, 0, 0, 0);
        assert(actual == input);
    }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'test.cc').write_text(harness.replace('// METHOD', body))
            subprocess.run(['c++', '-std=c++17', '-O1', '-g', '-fsanitize=address,undefined',
                            str(path / 'test.cc'), '-o', str(path / 'test')], check=True, timeout=60)
            subprocess.run([str(path / 'test')], check=True, timeout=30)

    def test_native_rate_releases_previous_resampler(self):
        source = (ROOT / 'main/audio/audio_service.cc').read_text()
        body = method(source, 'AudioService::SetDecodeSampleRate(')
        harness = r'''
#include <cassert>
#include <cstdint>
#include <mutex>
#include <vector>
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define OPUS_DEC_CFG(rate, duration) 0
#define RATE_CVT_CFG(src, dst, channels) 0
constexpr int OPUS_MAX_FRAME_DURATION_MS=60, ESP_AUDIO_MONO=1;
using esp_opus_dec_cfg_t=int;
using esp_ae_rate_cvt_cfg_t=int;
int closes=0, opens=0;
void esp_opus_dec_close(void*) {}
int esp_opus_dec_open(void*, size_t, void** handle) { *handle=(void*)1; return 0; }
void esp_ae_rate_cvt_close(void*) { ++closes; }
int esp_ae_rate_cvt_open(void*, void** handle) { *handle=(void*)2; ++opens; return 0; }
struct Codec { int output_sample_rate() { return 16000; } };
struct Board {
    Codec codec;
    static Board& GetInstance() { static Board b; return b; }
    Codec* GetAudioCodec() { return &codec; }
};
struct AudioService {
    std::mutex decoder_mutex_;
    int decoder_sample_rate_=24000, decoder_duration_ms_=40, decoder_frame_size_=1440;
    void* opus_decoder_=(void*)1;
    void* output_resampler_=(void*)2;
    std::vector<int16_t> resample_buffer_ = std::vector<int16_t>(3000);
    void SetDecodeSampleRate(int, int);
};
// METHOD
int main() {
    AudioService s;
    s.SetDecodeSampleRate(16000, 40);
    assert(closes == 1 && opens == 0);
    assert(s.output_resampler_ == nullptr);
    assert(s.resample_buffer_.capacity() == 0);
    assert(s.decoder_frame_size_ == 960);
    s.SetDecodeSampleRate(24000, 40);
    assert(opens == 1 && s.output_resampler_ != nullptr);
    s.SetDecodeSampleRate(24000, 40);
    assert(opens == 1 && closes == 1);
}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'test.cc').write_text(harness.replace('// METHOD', body))
            subprocess.run(['c++', '-std=c++17', '-pthread', '-O1', '-g',
                            '-fsanitize=address,undefined', str(path / 'test.cc'),
                            '-o', str(path / 'test')], check=True, timeout=60)
            subprocess.run([str(path / 'test')], check=True, timeout=30)
