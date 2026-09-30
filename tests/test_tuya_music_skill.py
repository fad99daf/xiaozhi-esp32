"""Regression checks for music cards delivered over Tuya MQTT."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def source(path):
    return (ROOT / path).read_text()


class TuyaMusicSkillTest(unittest.TestCase):
    def test_tuya_iot_client_registers_nonblocking_mqtt_message_callback(self):
        protocol = source("main/protocols/tuya_protocol.cc")
        self.assertIn("cfg.message_callback = OnMqttMessage", protocol)
        self.assertIn("void TuyaProtocol::OnMqttMessage", protocol)
        self.assertIn("Application::GetInstance().Schedule", protocol)

    def test_mqtt_callback_limits_pending_payloads(self):
        protocol = source("main/protocols/tuya_protocol.cc")
        callback = protocol[protocol.index("void TuyaProtocol::OnMqttMessage"):
                            protocol.index("bool TuyaProtocol::StartMqttPump")]
        self.assertIn("TryAcquire()", callback)
        self.assertIn("Release()", callback)
        self.assertLess(callback.index("TryAcquire()"), callback.index("Application::GetInstance().Schedule"))

    def test_music_worker_starts_only_for_tuya_protocol(self):
        audio = source("main/audio/audio_service.cc")
        start = audio[audio.index("void AudioService::Start()"):
                      audio.index("void AudioService::Stop()")]
        self.assertIn("#if CONFIG_PROTOCOL_TUYA", start)
        self.assertLess(start.index("#if CONFIG_PROTOCOL_TUYA"), start.index("music_player_->Start()"))

    def test_music_cancellation_never_aborts_shared_tts(self):
        player = source("main/audio/music_player.cc")
        audio = source("main/audio/audio_service.cc")
        self.assertIn("[this]() { CancelMusicPlayback(); }", audio)
        self.assertNotIn("AbortOutput()", player)
        self.assertIn("task->is_music = true", audio)
        cancel = audio[audio.index("void AudioService::CancelMusicPlayback()"):
                       audio.index("bool AudioService::IsIdle()")]
        self.assertIn("is_music", cancel)
        self.assertNotIn("audio_decode_queue_.clear()", cancel)

    def test_new_turn_pauses_and_advances_gate_atomically(self):
        player = source("main/audio/music_player.cc")
        begin = player[player.index("void MusicPlayer::BeginTurn()"):
                       player.index("void MusicPlayer::NotifyTtsStarted()")]
        self.assertIn("std::lock_guard<std::mutex> lock(request_mutex_)", begin)
        self.assertLess(begin.index("lock(request_mutex_)"), begin.index("gate_.BeginTurn()"))
        self.assertNotIn("Stop();", begin)

    def test_tts_decode_in_flight_takes_priority_over_music(self):
        audio = source("main/audio/audio_service.cc")
        output = audio[audio.index("void AudioService::AudioOutputTask()"):
                       audio.index("void AudioService::OpusCodecTask()")]
        decoder = audio[audio.index("void AudioService::ProcessDecodePacket("):
                        audio.index("void AudioService::PushTaskToEncodeQueue")]
        loop = audio[audio.index("void AudioService::OpusCodecLoop()"):
                     audio.index("void AudioService::ProcessDecodePacket(")]
        self.assertIn("decode_in_flight_ == 0", output)
        self.assertIn("decode_in_flight_++", loop)
        self.assertIn("decode_in_flight_--", loop)
        stale = decoder[decoder.index("if (generation != output_generation_.load())"):
                        decoder.index("auto ret = esp_opus_dec_decode")]
        self.assertIn("return;", stale)
        self.assertNotIn("audio_queue_mutex_", stale)

    def test_aborted_tts_also_pauses_music_state(self):
        player = source("main/audio/music_player.cc")
        aborted = player[player.index("void MusicPlayer::NotifyTtsAborted()"):
                         player.index("void MusicPlayer::Stop()")]
        self.assertIn("playback_state_.PauseForTts()", aborted)

    def test_resume_card_keeps_stream_and_does_not_require_urls(self):
        player = source("main/audio/music_player.cc")
        handler = player[player.index("bool MusicPlayer::HandleSkillCard"):
                         player.index("void MusicPlayer::BeginTurn()")]
        self.assertIn('strcmp(action, "resume") == 0', handler)
        self.assertLess(handler.index('strcmp(action, "resume") == 0'),
                        handler.index('cJSON_GetObjectItem(data, "audios")'))

    def test_tts_reset_preserves_paused_music_queue(self):
        audio = source("main/audio/audio_service.cc")
        reset = audio[audio.index("void AudioService::ResetDecoder()"):
                      audio.index("void AudioService::FlushAudioQueues()")]
        self.assertIn("audio_music_playback_queue_", audio)
        self.assertNotIn("audio_music_playback_queue_.clear()", reset)
        self.assertIn("music_paused_", audio)

    def test_music_worker_waits_for_output_tail(self):
        player = source("main/audio/music_player.cc")
        worker = player[player.index("void MusicPlayer::TaskLoop()"):
                        player.index("bool MusicPlayer::IsCancelled")]
        tail = worker[worker.index("for (const auto& url : urls)"):]
        self.assertIn("wait_for_output_()", tail)

    def test_tts_gate_releases_after_listening_state_transition(self):
        app = source("main/application.cc")
        callback = app[app.index('strcmp(state->valuestring, "stop") == 0'):
                       app.index('strcmp(state->valuestring, "abort") == 0')]
        self.assertLess(callback.index("SetDeviceState(kDeviceStateListening)"),
                        callback.index("NotifyMusicTtsFinished()"))
        self.assertGreaterEqual(callback.count("Schedule([this, generation]()"), 2)
        self.assertIn("generation != tts_generation_ || aborted_", callback)

    def test_mqtt_transport_uses_shared_skill_card_selector(self):
        app = source("main/application.cc")
        selector = source("main/protocols/tuya_mqtt_skill.cc")
        self.assertIn('"packet-type"', app)
        self.assertIn("SelectTuyaMqttSkillCard", app)
        self.assertIn('"protocol"', selector)
        self.assertIn("9000", selector)
        self.assertIn('"bizType"', selector)
        self.assertIn('"SKILL"', selector)
        self.assertIn('"skillCard"', selector)
        self.assertIn("HandleTuyaMusicSkill", app)

    def test_tai_skill_messages_are_forwarded_to_application_music_handler(self):
        protocol = source("main/protocols/tuya_protocol.cc")
        self.assertIn('strcmp(bizType->valuestring, "SKILL") == 0', protocol)
        self.assertIn('cJSON_AddStringToObject(out, "type", "skill")', protocol)

    def test_music_card_reads_general_or_custom_audio_playlist(self):
        player = source("main/audio/music_player.cc")
        self.assertIn('cJSON_GetObjectItem(root, "custom")', player)
        self.assertIn('cJSON_GetObjectItem(root, "general")', player)
        self.assertIn('cJSON_GetObjectItem(data, "audios")', player)
        self.assertIn('GetStringField(item, "url")', player)
        self.assertIn('GetStringField(item, "format")', player)
        self.assertIn('"play"', player)

    def test_playback_streams_http_mp3_into_audio_service_pcm_queue(self):
        player = source("main/audio/music_player.cc")
        audio = source("main/audio/audio_service.cc")
        self.assertIn("esp_http_client_open", player)
        self.assertIn("esp_audio_simple_dec_process", player)
        self.assertIn("pcm_sink_", player)
        self.assertIn("bool AudioService::PushPcmToPlaybackQueue", audio)

    def test_cancelled_music_does_not_continue_feeding_playback(self):
        player = source("main/audio/music_player.cc")
        self.assertIn("request_generation_", player)
        self.assertIn("IsCancelled", player)

if __name__ == "__main__":
    unittest.main()
