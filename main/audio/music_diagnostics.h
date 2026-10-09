#ifndef MUSIC_DIAGNOSTICS_H
#define MUSIC_DIAGNOSTICS_H

#include <cstdint>
#include <string>
#include <string_view>

// A log correlation key, not a security hash. Never log the signed URL itself.
inline uint32_t MusicResourceId(std::string_view url) {
    uint32_t hash = 2166136261U;
    for (unsigned char byte : url) hash = (hash ^ byte) * 16777619U;
    return hash;
}

// Bound cloud-controlled text and prevent injected newlines/control sequences.
inline std::string MusicLogText(const char* value, size_t limit = 96) {
    std::string text;
    if (!value) return text;
    for (size_t i = 0; value[i] && i < limit; ++i) {
        const unsigned char byte = value[i];
        text += byte < 0x20 || byte == 0x7f ? ' ' : value[i];
    }
    return text;
}

#endif
