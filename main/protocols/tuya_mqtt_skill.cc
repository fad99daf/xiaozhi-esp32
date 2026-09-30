#include "tuya_mqtt_skill.h"

#include <cstring>

#include <cJSON.h>

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
