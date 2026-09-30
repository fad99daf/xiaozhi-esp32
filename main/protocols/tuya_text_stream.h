#ifndef TUYA_TEXT_STREAM_H
#define TUYA_TEXT_STREAM_H

#include <cstddef>
#include <cstdint>
#include <string>

// TAI text frames may contain either a complete JSON document (even with a
// MIDDLE flag) or one fragment of a document. Keep only one bounded document.
class TuyaTextStream {
public:
    enum class Status { kPending, kComplete, kDropped };

    Status Feed(const char* data, size_t len, uint8_t stream_flag);
    const std::string& document() const { return buffer_; }
    void Reset();

private:
    // 16 accepted MP3 URLs can exceed 32 KiB including JSON metadata.
    static constexpr size_t kMaxDocumentBytes = 48 * 1024;
    std::string buffer_;
    int depth_ = 0;
    bool in_string_ = false;
    bool escaped_ = false;
    bool started_ = false;
    bool complete_ = false;
    bool dropping_ = false;
};

#endif
