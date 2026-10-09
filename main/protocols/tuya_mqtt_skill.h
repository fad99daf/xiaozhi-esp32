#ifndef TUYA_MQTT_SKILL_H
#define TUYA_MQTT_SKILL_H

#include <cstdint>
#include <string>

struct cJSON;

// Returns a borrowed card pointer. The caller must keep the parsed root alive.
const cJSON* SelectTuyaMqttSkillCard(const cJSON* message);
constexpr int TUYA_MUSIC_PAGE_SIZE = 8;
std::string BuildTuyaMusicListRequest(const std::string& biz_id, int64_t timestamp, int offset);
std::string BuildTuyaMusicUrlRequest(const std::string& biz_id, int64_t timestamp,
                                    const std::string& audio_id, const std::string& channel_code);

#endif
