#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <cerrno>
#include <cstdio>
#include <cstdarg>
#include <functional>
#include <mutex>
#include <string>
#include <vector>
#include <decoder/impl/esp_mp3_dec.h>
#include <simple_dec/esp_audio_simple_dec.h>
#include "music_playback_state.h"
#include "music_http_resume.h"
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
#define ESP_LOGE(...) TestLog(__VA_ARGS__)
#define MUSIC_HTTP_READ_SIZE 4096
#define MUSIC_ENCODED_BUFFER_LIMIT (64 * 1024)
#define MUSIC_DECODED_BUFFER_LIMIT 16384
#define ESP_OK 0
#define ESP_FAIL (-1)
#define ESP_ERR_HTTP_EAGAIN 0x7007
#define MUSIC_HTTP_MAX_RECONNECTS 3
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
#define MALLOC_CAP_INTERNAL 0
using esp_err_t = int;
const char* esp_err_to_name(int) { return "test"; }
unsigned heap_caps_get_free_size(int) { return 100000; }
int esp_crt_bundle_attach(void*) { return 0; }
static int64_t test_clock_us;
static unsigned pending_notifications;
std::function<void()> on_retry_wait;
int64_t esp_timer_get_time() { return test_clock_us; }
void vTaskDelay(unsigned ms) {
    test_clock_us += ms * 1000;
    if (on_retry_wait) on_retry_wait();
}
void ulTaskNotifyTake(int, unsigned ms) {
    vTaskDelay(ms);
    pending_notifications = 0;
}

enum { HTTP_EVENT_ON_HEADER };
struct esp_http_client_event_t {
    int event_id; void* user_data; const char* header_key; const char* header_value;
};
struct esp_http_client_config_t {
    const char* url; int timeout_ms; int buffer_size; int (*crt_bundle_attach)(void*);
    int (*event_handler)(esp_http_client_event_t*); void* user_data;
};

struct Http {
    std::vector<std::vector<uint8_t>> chunks;
    size_t next = 0;
    bool complete = true;
    size_t delivered = 0;
    unsigned opens = 0, cleanups = 0;
    int fail_at = -1, failures = 0, read_errno = ECONNRESET;
    int read_result = -1, open_failures = 0;
    int status = 200;
    bool ignore_range = false, wrong_range = false, changed_etag = false;
    bool omit_etag = false, unknown_length = false, corrupt_length = false;
    std::vector<size_t> ranges;
    std::vector<std::string> validators;
    esp_http_client_config_t cfg{};
} http;
using esp_http_client_handle_t = Http*;
size_t total_length() { size_t n = 0; for (const auto& c : http.chunks) n += c.size(); return n; }
esp_http_client_handle_t esp_http_client_init(esp_http_client_config_t* cfg) { http.cfg = *cfg; return &http; }
int esp_http_client_set_header(Http* h, const char* key, const char* value) {
    if (strcmp(key, "Range") == 0) h->ranges.push_back(std::stoull(std::string(value).substr(6)));
    if (strcmp(key, "If-Range") == 0) h->validators.emplace_back(value);
    if (strcmp(key, "Accept-Encoding") == 0) assert(strcmp(value, "identity") == 0);
    return ESP_OK;
}
int esp_http_client_open(Http* h, int) {
    ++h->opens;
    if (h->open_failures) { --h->open_failures; return ESP_FAIL; }
    return ESP_OK;
}
int response_status() { return http.opens > 1 && !http.ranges.empty() && !http.ignore_range ? 206 : http.status; }
int64_t esp_http_client_fetch_headers(Http* h) {
    auto header = [&](const char* key, const std::string& value) {
        if (!h->cfg.event_handler) return;
        esp_http_client_event_t event{HTTP_EVENT_ON_HEADER, h->cfg.user_data, key, value.c_str()};
        h->cfg.event_handler(&event);
    };
    if (!h->omit_etag) header("ETag", h->opens > 1 && h->changed_etag ? "\"changed\"" : "\"song\"");
    if (response_status() == 206) {
        const size_t offset = h->ranges.back();
        header("Content-Range", "bytes " + std::to_string(offset + h->wrong_range) + "-" +
               std::to_string(total_length() - 1) + "/" + std::to_string(total_length()));
    }
    if (h->opens > 1) {
        h->delivered = response_status() == 206 ? h->ranges.back() : 0;
        h->next = 0;
        size_t offset = h->delivered;
        while (h->next < h->chunks.size() && offset >= h->chunks[h->next].size()) {
            offset -= h->chunks[h->next++].size();
        }
        assert(offset == 0); // Recovery cases deliberately use HTTP chunk boundaries.
    }
    return h->unknown_length ? 0 : int64_t(total_length() - h->delivered + h->corrupt_length);
}
int esp_http_client_get_status_code(Http*) { return response_status(); }
int64_t esp_http_client_get_content_length(Http* h) {
    return h->unknown_length ? -1 : int64_t(total_length() - h->delivered + h->corrupt_length);
}
int esp_http_client_get_errno(Http* h) { return h->read_errno; }
bool esp_http_client_is_complete_data_received(Http* h) { return h->complete; }
int esp_http_client_read(Http* h, char* buffer, int size) {
    if (h->failures && int(h->next) == h->fail_at) {
        --h->failures; errno = h->read_errno; return h->read_result;
    }
    if (h->next == h->chunks.size()) return 0;
    const auto& chunk = h->chunks[h->next++];
    assert(chunk.size() <= size_t(size));
    memcpy(buffer, chunk.data(), chunk.size());
    h->delivered += chunk.size();
    return chunk.size();
}
void esp_http_client_close(Http*) {}
void esp_http_client_cleanup(Http* h) { ++h->cleanups; }

struct Decoder {
    std::vector<uint8_t> cached;
    bool header_done = false, eos = false, fail = false, stall = false;
    bool resize_seen = false, leave_tail = true;
    uint32_t required = 6000;
} decoder;
static int calls;
static bool registered;
static int decoder_opens, decoder_closes;
esp_audio_err_t esp_mp3_dec_register() { registered = true; return ESP_AUDIO_ERR_OK; }
const esp_audio_dec_ops_t* esp_audio_dec_get_ops(esp_audio_type_t) {
    static esp_audio_dec_ops_t ops = {};
    return registered ? &ops : nullptr;
}
esp_audio_err_t esp_audio_simple_dec_open(esp_audio_simple_dec_cfg_t* cfg, void** out) {
    assert(registered && cfg->dec_type == ESP_AUDIO_SIMPLE_DEC_TYPE_MP3 && !cfg->use_frame_dec);
    ++decoder_opens; *out = &decoder; return ESP_AUDIO_ERR_OK;
}
void esp_audio_simple_dec_close(void*) { ++decoder_closes; }
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
    std::mutex request_mutex_;
    MusicPlaybackState playback_state_;
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
    http = Http{}; http.chunks = {{'I'}, {'D', '3', 1}, {0, 2}, {0}};
    assert(player.StreamMp3("https://test/song.mp3?token=private-signature", 7, 9));
    assert((pcm == std::vector<int16_t>{1, 2}));
    assert(decoder.resize_seen && decoder.eos && decoder.cached.empty());
    assert(logs.find("reason=resource_eof") != std::string::npos);
    assert(logs.find("queued_samples=2") != std::string::npos);
    assert(logs.find("sample_rate=24000") != std::string::npos);
    assert(logs.find("gen=7 resource=") != std::string::npos);
    assert(logs.find("private-signature") == std::string::npos);
    assert(logs.find("https://test") == std::string::npos);

    auto reset = [&] {
        decoder = Decoder{}; calls = 0; pcm.clear(); logs.clear();
        decoder_opens = decoder_closes = 0; on_retry_wait = {};
        test_clock_us = 0; pending_notifications = 0; player.cancelled = false;
        player.playback_state_.StartTrack();
        http = Http{}; http.chunks = {{'I', 'D', '3', 1, 0, 2, 0}};
    };
    reset(); http.chunks = {{'I', 'D', '3', 1}, {0}, {2, 0}};
    http.fail_at = 2; http.failures = 1;
    assert(player.StreamMp3("https://test", 7, 9));
    assert((http.ranges == std::vector<size_t>{5}));
    assert((pcm == std::vector<int16_t>{1, 2}));
    assert(decoder_opens == 1 && decoder_closes == 1 && http.opens == 2);
    assert((http.validators == std::vector<std::string>{"\"song\""}));
    assert(http.cleanups == http.opens);

    reset(); http.chunks = {{'I', 'D', '3', 1}, {0}, {2, 0}};
    http.fail_at = 2; http.failures = 1; http.read_errno = ENOTCONN;
    assert(player.StreamMp3("https://test", 7, 9));
    assert((pcm == std::vector<int16_t>{1, 2}));
    auto interrupted = [&] {
        reset(); http.chunks = {{'I', 'D', '3', 1}, {0}, {2, 0}};
        http.fail_at = 2; http.failures = 1;
    };
    interrupted(); http.read_result = -ESP_ERR_HTTP_EAGAIN; http.read_errno = 0;
    assert(player.StreamMp3("https://test", 7, 9) && http.opens == 2);
    interrupted(); http.read_errno = EINVAL;
    assert(!player.StreamMp3("https://test", 7, 9) && http.opens == 1);
    reset(); http.open_failures = 1;
    assert(player.StreamMp3("https://test", 7, 9) && http.opens == 2 && http.ranges.empty());
    // Also recover after PCM has already reached the speaker queue, without
    // replaying those samples or losing the decoder's partially cached frame.
    reset(); http.chunks = {{'I', 'D', '3', 1, 0, 2, 0}, {3, 0, 4, 0}, {5, 0, 6, 0}};
    http.fail_at = 2; http.failures = 1;
    assert(player.StreamMp3("https://test", 7, 9));
    assert((http.ranges == std::vector<size_t>{11}));
    assert((pcm == std::vector<int16_t>{1, 2, 3, 4, 5, 6}));
    assert(decoder_opens == 1 && decoder_closes == 1);
    assert(logs.find("queued_samples=6") != std::string::npos);

    // A recovered network error is not the cause of a later decoder failure.
    interrupted();
    on_retry_wait = [&] { decoder.fail = true; };
    assert(!player.StreamMp3("https://test", 7, 9));
    const std::string decode_end = logs.substr(logs.rfind("Music track end:"));
    assert(decode_end.find("reason=decode_or_output") != std::string::npos);
    assert(decode_end.find("errno=0") != std::string::npos);

    interrupted(); http.failures = 10;
    assert(!player.StreamMp3("https://test", 7, 9));
    assert(http.opens == 4 && http.cleanups == 4 && http.ranges.size() == 3);
    assert(!decoder.eos && decoder_opens == 1 && decoder_closes == 1);
    assert(test_clock_us == 7000000);
    assert(logs.find("outcome=failed reason=reconnect_limit") != std::string::npos);

    for (int mismatch = 0; mismatch < 3; ++mismatch) {
        interrupted();
        http.ignore_range = mismatch == 0;
        http.wrong_range = mismatch == 1;
        http.changed_etag = mismatch == 2;
        assert(!player.StreamMp3("https://test", 7, 9));
        assert(http.opens == 2 && !decoder.eos && pcm.empty());
    }
    interrupted(); http.omit_etag = true;
    assert(!player.StreamMp3("https://test", 7, 9));
    assert(http.opens == 1 && !decoder.eos);

    reset(); http.fail_at = 0; http.failures = 1; http.omit_etag = true;
    assert(player.StreamMp3("https://test", 7, 9));
    assert(http.opens == 2 && http.ranges.empty()); // No body yet: restart safely without a validator.

    interrupted(); on_retry_wait = [&] { player.cancelled = true; };
    assert(!player.StreamMp3("https://test", 7, 9));
    assert(http.opens == 1 && http.cleanups == 1 && !decoder.eos);

    interrupted(); on_retry_wait = [&] {
        // A new playlist invalidates this stream and wakes TaskLoop. The
        // recovery delay must not consume the replacement playlist's wakeup.
        ++pending_notifications; player.cancelled = true;
    };
    assert(!player.StreamMp3("https://test", 7, 9));
    assert(pending_notifications == 1 && http.opens == 1);

    interrupted(); player.playback_state_.PauseForTts();
    on_retry_wait = [&] { player.cancelled = true; };
    assert(!player.StreamMp3("https://test", 7, 9));
    assert(http.opens == 1 && !decoder.eos); // Cancellation also wakes a paused recovery.

    interrupted(); player.playback_state_.PauseForTts();
    unsigned paused_ticks = 0;
    on_retry_wait = [&] {
        assert(http.opens == 1);
        if (++paused_ticks == 2) player.playback_state_.StartTrack();
    };
    assert(player.StreamMp3("https://test", 7, 9));
    assert(test_clock_us == 1200000 && http.opens == 2);

    reset(); http.unknown_length = true;
    assert(player.StreamMp3("https://test", 7, 9));
    interrupted(); http.unknown_length = true;
    assert(player.StreamMp3("https://test", 7, 9));
    assert((pcm == std::vector<int16_t>{1, 2}));

    reset(); http.corrupt_length = true;
    assert(!player.StreamMp3("https://test", 7, 9));
    assert(!decoder.eos); // Premature EOF must never flush decoder or request next song.
    reset(); http.status = 404;
    assert(!player.StreamMp3("https://test", 7, 9) && http.opens == 1 && decoder_opens == 0);

    MusicHttpHeaders headers;
    headers.Record("ETag", "W/\"weak\"");
    MusicHttpResume resume;
    assert(resume.AcceptResponse(200, 10, headers));
    resume.offset = 5;
    assert(!resume.CanReconnect());
    headers = {}; headers.Record("ETag", "\"same\"");
    resume = {}; assert(resume.AcceptResponse(200, 10, headers)); resume.offset = 5;
    headers.Record("Content-Range", "bytes 5-9/10");
    assert(resume.AcceptResponse(206, 5, headers));
    for (const char* range : {"bytes 4-9/10", "bytes 5-8/10", "bytes 5-9/11", "bytes 5-9/*",
                             "bytes 5-9/10 junk", "bytes 5-9/18446744073709551616"}) {
        headers.content_range = range;
        assert(!resume.AcceptResponse(206, 5, headers));
    }
    headers = {}; headers.Record("Content-Encoding", "gzip");
    assert(!MusicHttpResume{}.AcceptResponse(200, 10, headers));
    headers = {}; headers.Record("ETag", "\"same\""); headers.Record("etag", "\"other\"");
    assert(headers.invalid);
    reset(); decoder.fail = true;
    assert(!player.StreamMp3("https://test", 7, 9) && calls == 1);
    assert(http.opens == 1); // Decoder faults must not be retried as network failures.
    assert(logs.find("outcome=failed reason=decode_or_output") != std::string::npos);
    reset(); decoder.stall = true;
    assert(!player.StreamMp3("https://test", 7, 9) && calls < 3);
    reset(); decoder.required = 1000000;
    assert(!player.StreamMp3("https://test", 7, 9));
    reset(); http.complete = false;
    assert(!player.StreamMp3("https://test", 7, 9));
    reset(); http.chunks.clear();
    assert(!player.StreamMp3("https://test", 7, 9));  // Empty HTTP 200 is not a completed song.
    assert(logs.find("outcome=failed reason=empty_resource") != std::string::npos);
    reset(); player.pcm_sink_ = [&](std::vector<int16_t>&&, uint32_t) {
        player.cancelled = true; return false;
    };
    assert(!player.StreamMp3("https://test", 7, 9));
    assert(logs.find("outcome=cancelled reason=cancelled") != std::string::npos);
    assert(logs.find("queued_samples=0") != std::string::npos);
}
