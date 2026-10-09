#ifndef MUSIC_HTTP_RESUME_H
#define MUSIC_HTTP_RESUME_H

#include <cerrno>
#include <charconv>
#include <cstdint>
#include <cstring>
#include <string>
#include <strings.h>

// HTTP metadata is per connection; byte offset and validator are per track.
// No decoder/playback state belongs to this helper.
struct MusicHttpHeaders {
    std::string etag;
    std::string content_range;
    bool invalid = false;

    void Record(const char* key, const char* value) {
        if (!key || !value) return;
        if (strcasecmp(key, "ETag") == 0 || strcasecmp(key, "Content-Range") == 0) {
            if (strlen(value) > 256) { invalid = true; return; }
            auto& field = strcasecmp(key, "ETag") == 0 ? etag : content_range;
            if (!field.empty() && field != value) invalid = true;
            field = value;
        } else if (strcasecmp(key, "Content-Encoding") == 0 && strcasecmp(value, "identity") != 0) {
            invalid = true; // Range offsets must address the actual MP3 bytes.
        }
    }
};

struct MusicHttpResume {
    uint64_t offset = 0; // Bytes retained by the application, NOT playback time.
    int64_t total = -1;
    std::string etag;

    bool AcceptResponse(int status, int64_t length, const MusicHttpHeaders& headers) {
        if (headers.invalid) return false;
        if (offset == 0) {
            if (status != 200) return false;
            total = length;
            etag = StrongEtag(headers.etag) ? headers.etag : "";
            return true;
        }
        uint64_t start, end, size;
        if (status != 206 || etag.empty() || headers.etag != etag ||
            !ParseRange(headers.content_range, start, end, size) || start != offset ||
            end != size - 1 || size > INT64_MAX || (total >= 0 && size != uint64_t(total)) ||
            (length >= 0 && uint64_t(length) != end - start + 1)) return false;
        total = static_cast<int64_t>(size);
        return true;
    }

    bool CanReconnect() const {
        return offset == 0 || (!etag.empty() && (total < 0 || offset < uint64_t(total)));
    }

    bool CanAcceptBytes(size_t count) const {
        return uint64_t(count) <= UINT64_MAX - offset &&
               (total < 0 || (offset <= uint64_t(total) && count <= uint64_t(total) - offset));
    }

    bool AtEnd() const { return total < 0 || offset == uint64_t(total); }

    static bool TransientError(int error) {
        return error == 0 || error == ECONNRESET || error == ENOTCONN || error == ECONNABORTED ||
               error == ETIMEDOUT || error == EPIPE || error == EAGAIN || error == EWOULDBLOCK;
    }

private:
    static bool StrongEtag(const std::string& value) {
        if (value.size() < 2 || value.front() != '"' || value.back() != '"') return false;
        for (size_t i = 1; i + 1 < value.size(); ++i) {
            if (static_cast<unsigned char>(value[i]) < 0x21 || value[i] == '"' || value[i] == 0x7f) return false;
        }
        return true;
    }

    static bool ParseRange(const std::string& value, uint64_t& start, uint64_t& end, uint64_t& size) {
        if (value.compare(0, 6, "bytes ") != 0) return false;
        const char* pos = value.data() + 6;
        const char* last = value.data() + value.size();
        auto number = [&](uint64_t& out) {
            auto result = std::from_chars(pos, last, out);
            if (result.ec != std::errc()) return false;
            pos = result.ptr;
            return true;
        };
        if (!number(start) || pos == last || *pos++ != '-' || !number(end) ||
            pos == last || *pos++ != '/' || !number(size) || pos != last) return false;
        return start <= end && end < size;
    }
};

#endif
