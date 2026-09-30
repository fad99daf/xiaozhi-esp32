#include "tuya_text_stream.h"

#include <cctype>

void TuyaTextStream::Reset() {
    buffer_.clear();
    depth_ = 0;
    in_string_ = false;
    escaped_ = false;
    started_ = false;
    complete_ = false;
    dropping_ = false;
}

TuyaTextStream::Status TuyaTextStream::Feed(const char* data, size_t len, uint8_t stream_flag) {
    constexpr uint8_t kOneShot = 0;
    constexpr uint8_t kStart = 1;
    constexpr uint8_t kEnd = 3;
    const bool starts = stream_flag == kOneShot || stream_flag == kStart;
    const bool ends = stream_flag == kOneShot || stream_flag == kEnd;

    if (starts) Reset();
    if (dropping_) {
        if (ends) Reset();
        return Status::kPending;
    }
    if (complete_) Reset();
    if (!data && len != 0) {
        Reset();
        dropping_ = !ends;
        return Status::kDropped;
    }
    if (len > kMaxDocumentBytes - buffer_.size()) {
        Reset();
        dropping_ = !ends;
        return Status::kDropped;
    }

    for (size_t i = 0; i < len; ++i) {
        const char ch = data[i];
        if (complete_) {
            if (!std::isspace(static_cast<unsigned char>(ch))) {
                Reset();
                dropping_ = !ends;
                return Status::kDropped;
            }
        } else if (in_string_) {
            if (escaped_) escaped_ = false;
            else if (ch == '\\') escaped_ = true;
            else if (ch == '"') in_string_ = false;
        } else if (ch == '"') {
            in_string_ = true;
        } else if (ch == '{' || ch == '[') {
            if (!started_ && ch != '{') {
                Reset();
                dropping_ = !ends;
                return Status::kDropped;
            }
            started_ = true;
            ++depth_;
        } else if (ch == '}' || ch == ']') {
            if (!started_ || --depth_ < 0) {
                Reset();
                dropping_ = !ends;
                return Status::kDropped;
            }
            if (depth_ == 0) complete_ = true;
        } else if (!started_ && !std::isspace(static_cast<unsigned char>(ch))) {
            Reset();
            dropping_ = !ends;
            return Status::kDropped;
        }
    }
    if (len) buffer_.append(data, len);
    if (complete_) return Status::kComplete;
    if (ends && started_) {
        Reset();
        return Status::kDropped;
    }
    return Status::kPending;
}
