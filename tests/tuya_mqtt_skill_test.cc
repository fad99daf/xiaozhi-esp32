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
    TestIotAiEnvelope();
    TestLegacyAndUnrelatedMessages();
    TestPendingDeliveryIsBounded();
}
