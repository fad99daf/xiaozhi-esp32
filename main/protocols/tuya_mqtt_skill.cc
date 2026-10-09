#include "tuya_mqtt_skill.h"

#include <cstring>

#include <cJSON.h>

static std::string BuildMusicRequest(const std::string& biz_id, int64_t timestamp,
                                    const char* action, const char* key, const std::string& value,
                                    const std::string& channel_code = {}) {
    if (biz_id.empty() || biz_id.size() > 64 || timestamp < 0) return {};
    cJSON* root = cJSON_CreateObject();
    if (!root) return {};
    cJSON* body = cJSON_AddObjectToObject(root, "data");
    cJSON* params = body ? cJSON_AddObjectToObject(body, "data") : nullptr;
    // Polysense MqttBody reads parameters from data.data (Map<String,String>),
    // not a downstream skill card's general/data envelope.
    const bool ok = params && cJSON_AddNumberToObject(root, "protocol", 9000) &&
                    cJSON_AddNumberToObject(root, "t", static_cast<double>(timestamp)) &&
                    cJSON_AddStringToObject(body, "bizId", biz_id.c_str()) &&
                    cJSON_AddStringToObject(body, "bizType", "SKILL") &&
                    cJSON_AddStringToObject(params, "code", "PlayControl") &&
                    cJSON_AddStringToObject(params, "action", action) &&
                    cJSON_AddStringToObject(params, key, value.c_str()) &&
                    cJSON_AddStringToObject(params, "id", "0") &&
                    (strcmp(action, "music_list") == 0
                        ? cJSON_AddStringToObject(params, "limit", std::to_string(TUYA_MUSIC_PAGE_SIZE).c_str())
                        : cJSON_AddStringToObject(params, "bitrate", "128")) &&
                    (channel_code.empty() || cJSON_AddStringToObject(params, "channelCode", channel_code.c_str()));
    char* json = ok ? cJSON_PrintUnformatted(root) : nullptr;
    std::string result = json ? json : "";
    cJSON_free(json);
    cJSON_Delete(root);
    return result;
}

std::string BuildTuyaMusicListRequest(const std::string& biz_id, int64_t timestamp, int offset) {
    if (offset < 0) return {};
    return BuildMusicRequest(biz_id, timestamp, "music_list", "offset", std::to_string(offset));
}

std::string BuildTuyaMusicUrlRequest(const std::string& biz_id, int64_t timestamp,
                                    const std::string& audio_id, const std::string& channel_code) {
    // The cloud accepts comma-separated audioIds. This request resolves ONE
    // track, so reject commas/whitespace rather than resolving another ID.
    if (audio_id.empty() || audio_id.size() > 128 || channel_code.size() > 64) return {};
    for (unsigned char c : audio_id) if (c <= 0x20 || c == ',' || c == 0x7f) return {};
    return BuildMusicRequest(biz_id, timestamp, "refresh_play_url", "audioIds", audio_id, channel_code);
}

const cJSON* SelectTuyaMqttSkillCard(const cJSON* root) {
    if (!cJSON_IsObject(root)) return nullptr;

    const cJSON* message = root;
    const cJSON* protocol = cJSON_GetObjectItem(root, "protocol");
    if (protocol) {
        if (!cJSON_IsNumber(protocol) || protocol->valueint != 9000) return nullptr;
        message = cJSON_GetObjectItem(root, "data");
        if (!cJSON_IsObject(message)) return nullptr;
    }

    const cJSON* biz_type = cJSON_GetObjectItem(message, "bizType");
    const cJSON* card = nullptr;
    if (biz_type) {
        if (!cJSON_IsString(biz_type) ||
            std::strcmp(biz_type->valuestring, "SKILL") != 0) return nullptr;
        card = cJSON_GetObjectItem(message, "data");
    } else {
        card = cJSON_GetObjectItem(message, "skillCard");
        if (!cJSON_IsObject(card)) card = message;
    }

    const cJSON* code = cJSON_IsObject(card) ? cJSON_GetObjectItem(card, "code") : nullptr;
    return cJSON_IsString(code) ? card : nullptr;
}
