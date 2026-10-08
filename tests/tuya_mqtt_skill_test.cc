#include <cassert>
#include <cstring>

#include <cJSON.h>

#include "tuya_mqtt_skill.h"
#include "tuya_mqtt_delivery_limiter.h"

static void TestIotAiEnvelope() {
    const char* message = R"({"protocol":9000,"s":1,"t":1,"data":{"bizType":"SKILL","bizId":"card-1","data":{"code":"music","general":{"action":"play","data":{"preTtsFlag":true,"audios":[{"format":"mp3","url":"https://example.com/a.mp3"}]}}}}})";
    cJSON* root = cJSON_Parse(message);
    assert(root);
    const cJSON* card = SelectTuyaMqttSkillCard(root);
    assert(card);
    assert(std::strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(card, "code")), "music") == 0);
    cJSON_Delete(root);
}

static void TestLegacyAndUnrelatedMessages() {
    const char* messages[] = {
        R"({"bizType":"SKILL","data":{"code":"music"}})",
        R"({"skillCard":{"code":"music"}})",
        R"({"code":"music"})",
    };
    for (const char* message : messages) {
        cJSON* root = cJSON_Parse(message);
        assert(root && SelectTuyaMqttSkillCard(root));
        cJSON_Delete(root);
    }
    const char* rejected[] = {
        R"({"protocol":9001,"data":{"bizType":"SKILL","data":{"code":"music"}}})",
        R"({"protocol":9000,"data":{"bizType":"EVENT","data":{"code":"music"}}})",
        R"({"protocol":9000,"data":{"bizType":"SKILL","data":{"notCode":"music"}}})",
        R"({"bizType":"NLG","data":{"code":"music"}})",
    };
    for (const char* message : rejected) {
        cJSON* root = cJSON_Parse(message);
        assert(root && !SelectTuyaMqttSkillCard(root));
        cJSON_Delete(root);
    }
}

static void TestPendingDeliveryIsBounded() {
    TuyaMqttDeliveryLimiter limiter;
    assert(limiter.TryAcquire());
    assert(limiter.TryAcquire());
    assert(!limiter.TryAcquire());
    limiter.Release();
    assert(limiter.TryAcquire());
    limiter.Release();
    limiter.Release();
    assert(limiter.TryAcquire());
    limiter.Release();
}

int main() {
    const std::string request = BuildTuyaMusicNextRequest("next-test", 1234);
    cJSON* root = cJSON_Parse(request.c_str());
    assert(root && cJSON_GetObjectItem(root, "protocol")->valueint == 9000);
    const cJSON* body = cJSON_GetObjectItem(root, "data");
    const cJSON* params = cJSON_GetObjectItem(body, "data");
    assert(std::strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(body, "bizType")), "SKILL") == 0);
    assert(std::strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(body, "bizId")), "next-test") == 0);
    assert(std::strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(params, "code")), "PlayControl") == 0);
    assert(std::strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(params, "action")), "next") == 0);
    assert(std::strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(params, "auto")), "true") == 0);
    cJSON_Delete(root);
    assert(BuildTuyaMusicNextRequest("", 1234).empty());
    assert(BuildTuyaMusicNextRequest(std::string(65, 'x'), 1234).empty());
    assert(BuildTuyaMusicNextRequest("id", -1).empty());
    TestIotAiEnvelope();
    TestLegacyAndUnrelatedMessages();
    TestPendingDeliveryIsBounded();
}
