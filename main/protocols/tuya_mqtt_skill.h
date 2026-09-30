#ifndef TUYA_MQTT_SKILL_H
#define TUYA_MQTT_SKILL_H

struct cJSON;

// Returns a borrowed card pointer. The caller must keep the parsed root alive.
const cJSON* SelectTuyaMqttSkillCard(const cJSON* message);

#endif
