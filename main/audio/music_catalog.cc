#include "music_catalog.h"
#include "tuya_mqtt_skill.h"

#include <climits>
#include <cstring>
#include <cJSON.h>

static const char* StringField(const cJSON* object, const char* key) {
    const cJSON* field = cJSON_GetObjectItem(object, key);
    return cJSON_IsString(field) ? field->valuestring : nullptr;
}

static bool ValidAudioId(const char* id) {
    if (!id || !*id || strlen(id) > 128) return false;
    for (const unsigned char* p = (const unsigned char*)id; *p; ++p) {
        if (*p <= 0x20 || *p == ',' || *p == 0x7f) return false;
    }
    return true;
}

void MusicCatalog::Reset() {
    state_ = State::Ready; tracks_.clear(); next_ = 0; offset_ = 0; has_more_ = true;
}
void MusicCatalog::CancelPending() {
    if (state_ == State::AwaitPage || state_ == State::AwaitUrl) {
        state_ = State::Finished;
        tracks_.clear();
    }
}
bool MusicCatalog::HasNext() const {
    return state_ == State::Ready && (next_ < tracks_.size() || has_more_);
}
std::string MusicCatalog::Request(const std::string& biz_id, int64_t timestamp) {
    if (!HasNext()) return {};
    const bool resolve_url = next_ < tracks_.size();
    std::string request = resolve_url
        ? BuildTuyaMusicUrlRequest(biz_id, timestamp, tracks_[next_].audio_id, tracks_[next_].channel_code)
        : BuildTuyaMusicListRequest(biz_id, timestamp, offset_);
    if (!request.empty()) state_ = resolve_url ? State::AwaitUrl : State::AwaitPage;
    return request;
}

MusicCatalog::Result MusicCatalog::HandleResponse(const char* action, const cJSON* data, std::string& url) {
    url.clear();
    if (!action) return Result::Ignored;
    const bool page_response = strcmp(action, "music_list") == 0 && state_ == State::AwaitPage;
    const bool url_response = strcmp(action, "refresh_play_url") == 0 && state_ == State::AwaitUrl;
    if (!page_response && !url_response) return Result::Ignored;
    auto fail = [&] { state_ = State::Finished; tracks_.clear(); return Result::Failed; };
    if (!cJSON_IsObject(data)) return fail();
    if (page_response) {
        const cJSON* page = cJSON_GetObjectItem(data, "page");
        const cJSON* offset = cJSON_GetObjectItem(page, "offset");
        // Discard a demonstrably unrelated response BEFORE validating its
        // error flags or resource bounds; it cannot fail our pending request.
        if (cJSON_IsNumber(offset) && offset->valuedouble != offset_) return Result::Ignored;
    }
    const cJSON* items = cJSON_GetObjectItem(data, "items");
    if (!cJSON_IsArray(items)) return fail();
    const int count = cJSON_GetArraySize(items);
    if (url_response && count > 0) {
        bool matches = false;
        // The parent MQTT/TAI JSON size is bounded. Inspect IDs without copying
        // unrelated batches (the cloud also supports multi-ID URL requests).
        const cJSON* item = nullptr;
        cJSON_ArrayForEach(item, items) {
            const char* id = StringField(item, "audioId");
            if (id && tracks_[next_].audio_id == id) { matches = true; break; }
        }
        if (!matches) return Result::Ignored;
    }
    if (cJSON_IsFalse(cJSON_GetObjectItem(data, "success")) || count > TUYA_MUSIC_PAGE_SIZE) return fail();

    if (page_response) {
        const cJSON* page = cJSON_GetObjectItem(data, "page");
        const cJSON* offset = cJSON_GetObjectItem(page, "offset");
        const cJSON* limit = cJSON_GetObjectItem(page, "limit");
        const cJSON* more = cJSON_GetObjectItem(page, "hasMore");
        if (!cJSON_IsNumber(offset) || !cJSON_IsNumber(limit) ||
            limit->valuedouble != TUYA_MUSIC_PAGE_SIZE || (more && !cJSON_IsBool(more))) return fail();
        // Cloud bizId is regenerated. The outstanding action and exact page
        // offset reject unrelated/duplicate responses without guessing IDs.
        if (offset->valuedouble != offset_) return Result::Ignored;
        if (count == 0) {
            state_ = State::Finished; tracks_.clear(); return Result::Finished;
        }
        if (offset_ > INT_MAX - count) return fail();
        std::vector<Track> tracks;
        for (int i = 0; i < count; ++i) {
            const cJSON* item = cJSON_GetArrayItem(items, i);
            const char* id = StringField(item, "audioId");
            const char* channel = StringField(item, "channelCode");
            if (!ValidAudioId(id) || (channel && strlen(channel) > 64)) continue;
            bool duplicate = false;
            for (const auto& existing : tracks) {
                if (existing.audio_id == id && existing.channel_code == (channel ? channel : "")) duplicate = true;
            }
            if (!duplicate) tracks.push_back({id, channel ? channel : ""});
        }
        if (tracks.empty()) return fail(); // No unbounded fetching of unusable pages.
        tracks_ = std::move(tracks);
        next_ = 0;
        offset_ += count; // Raw rows, not filtered rows, determine pagination.
        has_more_ = more ? cJSON_IsTrue(more) : count == TUYA_MUSIC_PAGE_SIZE;
        state_ = State::Ready;
        return Result::NeedRequest;
    }

    if (count == 0) return fail();
    for (int i = 0; i < count; ++i) {
        const cJSON* item = cJSON_GetArrayItem(items, i);
        const char* id = StringField(item, "audioId");
        if (!id || tracks_[next_].audio_id != id) continue;
        const char* value = StringField(item, "url");
        const char* format = StringField(item, "format");
        if (!cJSON_IsTrue(cJSON_GetObjectItem(item, "success")) || !value || !format ||
            strcmp(format, "mp3") != 0 || strlen(value) >= 2048 ||
            (strncmp(value, "https://", 8) != 0 && strncmp(value, "http://", 7) != 0)) return fail();
        url = value;
        ++next_;
        state_ = State::Ready;
        return Result::Playable;
    }
    return Result::Ignored;
}
