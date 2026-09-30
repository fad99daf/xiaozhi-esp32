#include <cassert>
#include <cstring>
#include <string>

#include "music_start_gate.h"
#include "music_playback_state.h"
#include "tuya_text_stream.h"

static void TestTaiTextDocuments() {
    TuyaTextStream stream;
    const std::string music = R"({"bizType":"SKILL","data":{"code":"music","general":{"data":{"audios":[{"url":"https://example.com/a.mp3"}]}}}})";

    // This server sends complete JSON documents in MIDDLE frames.
    assert(stream.Feed(music.data(), music.size(), 2) == TuyaTextStream::Status::kComplete);
    assert(stream.document() == music);
    stream.Reset();

    // The SDK also permits one JSON document split across several callbacks.
    auto first = music.substr(0, 27);
    auto second = music.substr(27, 43);
    auto last = music.substr(70);
    assert(stream.Feed(first.data(), first.size(), 1) == TuyaTextStream::Status::kPending);
    assert(stream.Feed(second.data(), second.size(), 2) == TuyaTextStream::Status::kPending);
    assert(stream.Feed(last.data(), last.size(), 3) == TuyaTextStream::Status::kComplete);
    assert(stream.document() == music);
    stream.Reset();

    const std::string escaped = R"({"text":"escaped \\\" brace } inside string"})";
    assert(stream.Feed(escaped.data(), escaped.size(), 0) == TuyaTextStream::Status::kComplete);
    stream.Reset();

    assert(stream.Feed(first.data(), first.size(), 1) == TuyaTextStream::Status::kPending);
    assert(stream.Feed(music.data(), music.size(), 1) == TuyaTextStream::Status::kComplete);
    assert(stream.document() == music);
    stream.Reset();

    assert(stream.Feed(first.data(), first.size(), 1) == TuyaTextStream::Status::kPending);
    assert(stream.Feed(nullptr, 0, 3) == TuyaTextStream::Status::kDropped);
    assert(stream.Feed(music.data(), music.size(), 2) == TuyaTextStream::Status::kComplete);
    stream.Reset();

    // A valid skill card may carry multiple URL entries beyond 8 KiB.
    std::string long_card = "{\"bizType\":\"SKILL\",\"data\":{\"code\":\"music\",\"pad\":\"" +
                            std::string(9000, 'x') + "\"}}";
    assert(stream.Feed(long_card.data(), long_card.size(), 0) == TuyaTextStream::Status::kComplete);
    stream.Reset();

    std::string oversized(50000, 'x');
    assert(stream.Feed(oversized.data(), oversized.size(), 1) == TuyaTextStream::Status::kDropped);
    assert(stream.Feed(music.data(), music.size(), 2) == TuyaTextStream::Status::kPending);
    assert(stream.Feed(music.data(), music.size(), 1) == TuyaTextStream::Status::kComplete);
}

static void TestMusicWaitsForLaterTts() {
    MusicStartGate gate;
    assert(!gate.TtsActive());
    auto preamble = gate.Arm(true);
    assert(!gate.Ready(preamble));  // Empty audio queues cannot satisfy preTtsFlag.
    gate.NotifyTtsFinished();
    assert(gate.Ready(preamble));

    auto late_card = gate.Arm(true);
    assert(gate.Ready(late_card));  // MQTT can arrive after this turn's TTS stop.

    gate.BeginTurn();
    auto next_turn = gate.Arm(true);
    assert(!gate.Ready(next_turn));  // Previous turn's TTS cannot release this one.
    gate.NotifyTtsStarted();
    assert(gate.TtsActive());
    assert(!gate.Ready(next_turn));
    gate.NotifyTtsAborted();
    assert(!gate.TtsActive());
    assert(gate.Expired(next_turn));

    gate.BeginTurn();
    auto current_turn = gate.Arm(true);
    gate.NotifyTtsStarted();
    gate.NotifyTtsFinished();
    assert(gate.Ready(current_turn));
    assert(gate.Expired(next_turn));
    assert(gate.Ready(gate.Arm(false)));
}

static void TestResumePreservesTheActiveStream() {
    MusicStartGate gate;
    MusicPlaybackState playback;
    assert(!playback.RequestResume(gate, true));
    playback.StartTrack();
    assert(playback.PauseForTurn());
    gate.BeginTurn();
    assert(playback.paused());
    assert(playback.RequestResume(gate, true));
    assert(!playback.OnTtsFinished(gate));
    gate.NotifyTtsStarted();
    gate.NotifyTtsFinished();
    assert(playback.OnTtsFinished(gate));
    assert(!playback.paused());
    playback.Stop();
    assert(!playback.RequestResume(gate, false));
}

static void TestInterruptedReplyDoesNotReleaseMusic() {
    MusicStartGate gate;
    MusicPlaybackState playback;
    playback.StartTrack();
    assert(playback.PauseForTurn());
    gate.BeginTurn();
    assert(playback.RequestResume(gate, true));
    gate.NotifyTtsAborted();
    assert(!playback.OnTtsFinished(gate));
    assert(playback.paused());
}

int main() {
    TestTaiTextDocuments();
    TestMusicWaitsForLaterTts();
    TestResumePreservesTheActiveStream();
    TestInterruptedReplyDoesNotReleaseMusic();
}
