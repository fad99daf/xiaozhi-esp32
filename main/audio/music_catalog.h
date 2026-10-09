#ifndef MUSIC_CATALOG_H
#define MUSIC_CATALOG_H

#include <cstdint>
#include <string>
#include <vector>

struct cJSON;

// One bounded page of cloud metadata. Caller serializes access. HTTP/decode,
// task scheduling and TTS ownership stay in MusicPlayer, not in this helper.
class MusicCatalog {
public:
    enum class Result { Ignored, NeedRequest, Playable, Finished, Failed };
    void Reset();
    void CancelPending();
    bool HasNext() const;
    std::string Request(const std::string& biz_id, int64_t timestamp);
    Result HandleResponse(const char* action, const cJSON* data, std::string& url);

private:
    enum class State { Finished, Ready, AwaitPage, AwaitUrl };
    struct Track { std::string audio_id, channel_code; };
    State state_ = State::Finished;
    std::vector<Track> tracks_;
    size_t next_ = 0;
    int offset_ = 0;
    bool has_more_ = true;
};

#endif
