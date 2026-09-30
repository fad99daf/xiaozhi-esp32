#ifndef MUSIC_START_GATE_H
#define MUSIC_START_GATE_H

#include <atomic>
#include <cstdint>

// preTtsFlag waits for the current turn's TTS, whether the card arrives before
// or after TTS completion. The turn epoch prevents an older completion from
// releasing a card for a new turn.
class MusicStartGate {
public:
    struct Ticket {
        bool wait_for_tts;
        uint32_t turn_epoch;
    };

    void BeginTurn() {
        uint32_t old = state_.load();
        while (!state_.compare_exchange_weak(old, (old & ~kStateMask) + kTurnStep)) {}
    }
    void NotifyTtsStarted() { SetState(kStarted); }
    void NotifyTtsFinished() { SetState(kCompleted); }
    void NotifyTtsAborted() { SetState(kAborted); }
    Ticket Arm(bool wait_for_tts) const {
        return {wait_for_tts, state_.load() & ~kStateMask};
    }
    bool Ready(Ticket ticket) const {
        return !ticket.wait_for_tts || state_.load() == (ticket.turn_epoch | kCompleted);
    }
    bool TtsActive() const { return (state_.load() & kStateMask) == kStarted; }
    bool Expired(Ticket ticket) const {
        if (!ticket.wait_for_tts) return false;
        const uint32_t current = state_.load();
        return (current & ~kStateMask) != ticket.turn_epoch ||
               (current & kStateMask) == kAborted;
    }

private:
    static constexpr uint32_t kStateMask = 3;
    static constexpr uint32_t kTurnStep = 4;
    static constexpr uint32_t kStarted = 1;
    static constexpr uint32_t kCompleted = 2;
    static constexpr uint32_t kAborted = 3;

    void SetState(uint32_t value) {
        uint32_t old = state_.load();
        while (!state_.compare_exchange_weak(old, (old & ~kStateMask) | value)) {}
    }

    std::atomic<uint32_t> state_{0};
};

#endif
