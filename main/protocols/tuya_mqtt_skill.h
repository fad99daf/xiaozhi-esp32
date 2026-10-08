#ifndef TUYA_MQTT_SKILL_H
#define TUYA_MQTT_SKILL_H

#include <cstdint>
#include <string>

struct cJSON;

// Returns a borrowed card pointer. The caller must keep the parsed root alive.
const cJSON* SelectTuyaMqttSkillCard(const cJSON* message);
std::string BuildTuyaMusicNextRequest(const std::string& biz_id, int64_t timestamp);

#endif
