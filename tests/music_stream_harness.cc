#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <functional>
#include <string>
#include <vector>
#include <decoder/impl/esp_mp3_dec.h>
#include <simple_dec/esp_audio_simple_dec.h>

#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define MUSIC_HTTP_READ_SIZE 4096
#define MUSIC_ENCODED_BUFFER_LIMIT (64 * 1024)
#define MUSIC_DECODED_BUFFER_LIMIT 16384
#define ESP_OK 0
#define MALLOC_CAP_INTERNAL 0
using esp_err_t = int;
const char* esp_err_to_name(int) { return "test"; }
unsigned heap_caps_get_free_size(int) { return 100000; }
int esp_crt_bundle_attach(void*) { return 0; }

struct Http {
    std::vector<std::vector<uint8_t>> chunks;
    size_t next = 0;
    bool complete = true;
} http;
using esp_http_client_handle_t = Http*;
struct esp_http_client_config_t {
    const char* url; int timeout_ms; int buffer_size; int (*crt_bundle_attach)(void*);
};
esp_http_client_handle_t esp_http_client_init(esp_http_client_config_t*) { return &http; }
int esp_http_client_open(Http*, int) { return 0; }
int esp_http_client_fetch_headers(Http*) { return 0; }
int esp_http_client_get_status_code(Http*) { return 200; }
bool esp_http_client_is_complete_data_received(Http* h) { return h->complete; }
int esp_http_client_read(Http* h, char* buffer, int size) {
    if (h->next == h->chunks.size()) return 0;
    const auto& chunk = h->chunks[h->next++];
    assert(chunk.size() <= size_t(size));
    memcpy(buffer, chunk.data(), chunk.size());
    return chunk.size();
}
void esp_http_client_close(Http*) {}
void esp_http_client_cleanup(Http*) {}

struct Decoder {
    std::vector<uint8_t> cached;
    bool header_done = false, eos = false, fail = false, stall = false;
    bool resize_seen = false, leave_tail = true;
    uint32_t required = 6000;
} decoder;
static int calls;
static bool registered;
esp_audio_err_t esp_mp3_dec_register() { registered = true; return ESP_AUDIO_ERR_OK; }
const esp_audio_dec_ops_t* esp_audio_dec_get_ops(esp_audio_type_t) {
    static esp_audio_dec_ops_t ops = {};
    return registered ? &ops : nullptr;
}
esp_audio_err_t esp_audio_simple_dec_open(esp_audio_simple_dec_cfg_t* cfg, void** out) {
    assert(registered && cfg->dec_type == ESP_AUDIO_SIMPLE_DEC_TYPE_MP3 && !cfg->use_frame_dec);
    *out = &decoder; return ESP_AUDIO_ERR_OK;
}
void esp_audio_simple_dec_close(void*) {}
esp_audio_err_t esp_audio_simple_dec_get_info(void*, esp_audio_simple_dec_info_t* info) {
    info->sample_rate = 24000; info->channel = 1; info->bits_per_sample = 16;
    return ESP_AUDIO_ERR_OK;
}
esp_audio_err_t esp_audio_simple_dec_process(void*, esp_audio_simple_dec_raw_t* raw,
                                          esp_audio_simple_dec_out_t* out) {
    assert(++calls < 100); // Detect no-progress loops, including at EOF.
    if (decoder.fail) return ESP_AUDIO_ERR_FAIL;
    if (decoder.stall) return ESP_AUDIO_ERR_OK;
    if (out->len < decoder.required) {
        decoder.resize_seen = true; out->needed_size = decoder.required;
        return ESP_AUDIO_ERR_BUFF_NOT_ENOUGH;
    }
    // Leave one input byte unconsumed once, to exercise caller preservation.
    raw->consumed = raw->len;
    if (decoder.leave_tail && raw->len > 1) {
        --raw->consumed; decoder.leave_tail = false;
    }
    decoder.cached.insert(decoder.cached.end(), raw->buffer, raw->buffer + raw->consumed);
    decoder.eos = decoder.eos || raw->eos;
    if (!decoder.header_done && decoder.cached.size() >= 3) {
        assert(std::string(decoder.cached.begin(), decoder.cached.begin() + 3) == "ID3");
        decoder.cached.erase(decoder.cached.begin(), decoder.cached.begin() + 3);
        decoder.header_done = true;
    }
    // Emit two little-endian samples only once the fake frame is complete.
    if (decoder.header_done && decoder.cached.size() >= 4) {
        memcpy(out->buffer, decoder.cached.data(), 4); out->decoded_size = 4;
        decoder.cached.erase(decoder.cached.begin(), decoder.cached.begin() + 4);
    }
    return ESP_AUDIO_ERR_OK;
}
// The old raw path has no stream parser and rejects our metadata/fragments.
esp_audio_err_t esp_mp3_dec_open(void*, uint32_t, void** out) { *out = &decoder; return ESP_AUDIO_ERR_OK; }
esp_audio_err_t esp_mp3_dec_close(void*) { return ESP_AUDIO_ERR_OK; }
esp_audio_err_t esp_mp3_dec_decode(void*, esp_audio_dec_in_raw_t*, esp_audio_dec_out_frame_t*, esp_audio_dec_info_t*) {
    ++calls; return ESP_AUDIO_ERR_NOT_SUPPORT;
}

using esp_ae_rate_cvt_handle_t = void*;
using esp_ae_sample_t = void*;
struct esp_ae_rate_cvt_cfg_t {
    uint32_t src_rate, dest_rate, channel, bits_per_sample, complexity, perf_type;
};
#define ESP_AE_ERR_OK 0
#define ESP_AE_RATE_CVT_PERF_TYPE_SPEED 0
int esp_ae_rate_cvt_open(esp_ae_rate_cvt_cfg_t*, void**) { assert(false); return -1; }
void esp_ae_rate_cvt_close(void*) {}
int esp_ae_rate_cvt_get_max_out_sample_num(void*, uint32_t, uint32_t*) { return -1; }
int esp_ae_rate_cvt_process(void*, void*, uint32_t, void*, uint32_t*) { return -1; }
struct Codec { int output_sample_rate() { return 24000; } } codec;
struct MusicPlayer {
    Codec* codec_ = &codec;
    bool cancelled = false;
    std::function<bool(std::vector<int16_t>&&, uint32_t)> pcm_sink_;
    bool IsCancelled(uint32_t) const { return cancelled; }
    // DECLARATIONS
};
// METHODS

int main() {
    MusicPlayer player;
    std::vector<int16_t> pcm;
    player.pcm_sink_ = [&](std::vector<int16_t>&& samples, uint32_t generation) {
        assert(generation == 9);
        pcm.insert(pcm.end(), samples.begin(), samples.end()); return true;
    };
    http = {{{'I'}, {'D', '3', 1}, {0, 2}, {0}}};
    assert(player.StreamMp3("https://test", 7, 9));
    assert((pcm == std::vector<int16_t>{1, 2}));
    assert(decoder.resize_seen && decoder.eos && decoder.cached.empty());

    auto reset = [&] {
        decoder = Decoder{}; calls = 0; pcm.clear();
        http = {{{'I', 'D', '3', 1, 0, 2, 0}}};
    };
    reset(); decoder.fail = true;
    assert(!player.StreamMp3("https://test", 7, 9) && calls == 1);
    reset(); decoder.stall = true;
    assert(!player.StreamMp3("https://test", 7, 9) && calls < 3);
    reset(); decoder.required = 1000000;
    assert(!player.StreamMp3("https://test", 7, 9));
    reset(); http.complete = false;
    assert(!player.StreamMp3("https://test", 7, 9));
    reset(); http.chunks.clear();
    assert(!player.StreamMp3("https://test", 7, 9));  // Empty HTTP 200 is not a completed song.
    reset(); player.pcm_sink_ = [&](std::vector<int16_t>&&, uint32_t) {
        player.cancelled = true; return false;
    };
    assert(!player.StreamMp3("https://test", 7, 9));
}
