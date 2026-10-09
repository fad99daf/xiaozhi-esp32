#include <cassert>
#include <cstring>
#include <string>
#include <cJSON.h>
#include "music_catalog.h"

struct Json {
    cJSON* value;
    explicit Json(const char* text) : value(cJSON_Parse(text)) { assert(value); }
    ~Json() { cJSON_Delete(value); }
};

static void CheckRequest(const std::string& request, const char* action, const char* key, const char* value) {
    Json json(request.c_str());
    const cJSON* data = cJSON_GetObjectItem(cJSON_GetObjectItem(json.value, "data"), "data");
    assert(std::strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(data, "action")), action) == 0);
    assert(std::strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(data, key)), value) == 0);
}

int main() {
    using Result = MusicCatalog::Result;
    MusicCatalog catalog;
    std::string url;
    Json page(R"({"listId":"list-1","page":{"offset":0,"limit":8,"total":-1,"hasMore":true},"items":[{"audioId":"a","name":"A","channelCode":"demo","playUrl":""},{"audioId":"b","name":"B","channelCode":"demo","playUrl":""}]})");
    assert(catalog.HandleResponse("music_list", page.value, url) == Result::Ignored);
    catalog.Reset();
    CheckRequest(catalog.Request("page", 1000), "music_list", "offset", "0");
    assert(catalog.Request("duplicate", 1000).empty()); // One pending operation.
    std::string unrelated = R"({"success":false,"page":{"offset":24,"limit":16,"hasMore":true},"items":[)";
    for (int i = 0; i < 9; ++i) unrelated += (i ? "," : "") + std::string(R"({"audioId":"other"})");
    unrelated += "]}";
    Json other_page(unrelated.c_str());
    assert(catalog.HandleResponse("music_list", other_page.value, url) == Result::Ignored);
    assert(catalog.HandleResponse("music_list", page.value, url) == Result::NeedRequest);
    CheckRequest(catalog.Request("url-a", 1000), "refresh_play_url", "audioIds", "a");
    assert(catalog.HandleResponse("music_list", page.value, url) == Result::Ignored);
    Json wrong(R"({"success":true,"items":[{"audioId":"wrong","success":true,"url":"https://test/wrong.mp3","format":"mp3"}]})");
    assert(catalog.HandleResponse("refresh_play_url", wrong.value, url) == Result::Ignored);
    unrelated = R"({"success":false,"items":[)";
    for (int i = 0; i < 9; ++i) unrelated += (i ? "," : "") + std::string(R"({"audioId":"other"})");
    unrelated += "]}";
    Json other_urls(unrelated.c_str());
    assert(catalog.HandleResponse("refresh_play_url", other_urls.value, url) == Result::Ignored);
    unrelated = R"({"items":[)";
    for (int i = 0; i < 20000; ++i) unrelated += i ? ",{}" : "{}";
    unrelated += "]}";
    Json large_unrelated(unrelated.c_str()); // A large but valid <=64 KiB MQTT body.
    assert(catalog.HandleResponse("refresh_play_url", large_unrelated.value, url) == Result::Ignored);
    Json first(R"({"success":true,"items":[{"audioId":"a","success":true,"url":"https://test/a.mp3","format":"mp3"}]})");
    assert(catalog.HandleResponse("refresh_play_url", first.value, url) == Result::Playable);
    assert(url == "https://test/a.mp3");
    // No cloud page request while another local item is available.
    CheckRequest(catalog.Request("url-b", 1001), "refresh_play_url", "audioIds", "b");
    Json second(R"({"success":true,"items":[{"audioId":"b","success":true,"url":"https://test/b.mp3","format":"mp3"}]})");
    assert(catalog.HandleResponse("refresh_play_url", second.value, url) == Result::Playable);
    CheckRequest(catalog.Request("next-page", 1002), "music_list", "offset", "2");
    assert(catalog.HandleResponse("music_list", page.value, url) == Result::Ignored); // Old offset.
    Json end(R"({"page":{"offset":2,"limit":8,"hasMore":false},"items":[]})");
    assert(catalog.HandleResponse("music_list", end.value, url) == Result::Finished);
    assert(!catalog.HasNext() && catalog.Request("finished", 1003).empty());

    catalog.Reset(); catalog.Request("page", 1000);
    Json final_page(R"({"page":{"offset":0,"limit":8,"hasMore":false},"items":[{"audioId":"a","channelCode":"demo"},{"audioId":"a","channelCode":"demo"}]})");
    assert(catalog.HandleResponse("music_list", final_page.value, url) == Result::NeedRequest);
    catalog.Request("url", 1000);
    assert(catalog.HandleResponse("refresh_play_url", first.value, url) == Result::Playable);
    assert(!catalog.HasNext()); // Duplicate rows do not play twice; no fetch after hasMore=false.

    catalog.Reset(); catalog.Request("page", 1000); catalog.CancelPending();
    assert(catalog.HandleResponse("music_list", page.value, url) == Result::Ignored);
    assert(!catalog.HasNext()); // Cancelled/timed-out work cannot start music later.

    catalog.Reset(); catalog.Request("page", 1000);
    assert(catalog.HandleResponse("music_list", final_page.value, url) == Result::NeedRequest);
    catalog.Request("url", 1000);
    Json failure(R"({"success":true,"items":[{"audioId":"a","success":false,"url":"","format":"mp3"}]})");
    assert(catalog.HandleResponse("refresh_play_url", failure.value, url) == Result::Failed);
    assert(!catalog.HasNext()); // Failure does not silently skip to another song.

    catalog.Reset(); catalog.Request("page", 1000);
    Json malformed(R"({"page":{"offset":0,"limit":8,"hasMore":true},"items":[{"audioId":12},{"audioId":"one,two"}]})");
    assert(catalog.HandleResponse("music_list", malformed.value, url) == Result::Failed);
    assert(!catalog.HasNext());
}
