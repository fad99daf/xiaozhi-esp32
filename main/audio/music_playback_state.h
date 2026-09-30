#ifndef MUSIC_PLAYBACK_STATE_H
#define MUSIC_PLAYBACK_STATE_H

#include "music_start_gate.h"

// Only the control state lives here. The music worker retains its HTTP stream,
// MP3 decoder and resampler while paused, so resume never has to guess a byte
// offset in a VBR MP3 file.
class MusicPlaybackState {
public:
    void StartTrack() {
        state_ = State::kPlaying;
        resume_requested_ = false;
    }

    bool PauseForTurn() {
        resume_requested_ = false;
        return PauseForTts();
    }

    bool PauseForTts() {
        if (state_ == State::kStopped) return false;
        state_ = State::kPaused;
        return true;
    }

    bool RequestResume(const MusicStartGate& gate, bool wait_for_tts) {
        if (state_ == State::kStopped) return false;
        if (state_ == State::kPlaying) return true;
        resume_ticket_ = gate.Arm(wait_for_tts);
        resume_requested_ = true;
        return true;
    }

    bool OnTtsFinished(const MusicStartGate& gate) {
        if (state_ != State::kPaused || !resume_requested_ ||
            !gate.Ready(resume_ticket_) || gate.TtsActive()) return false;
        state_ = State::kPlaying;
        resume_requested_ = false;
        return true;
    }

    void Stop() {
        state_ = State::kStopped;
        resume_requested_ = false;
    }

    bool active() const { return state_ != State::kStopped; }
    bool paused() const { return state_ == State::kPaused; }

private:
    enum class State { kStopped, kPlaying, kPaused };
    State state_ = State::kStopped;
    bool resume_requested_ = false;
    MusicStartGate::Ticket resume_ticket_{false, 0};
};

#endif
