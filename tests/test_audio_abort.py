"""Compile actual AudioService worker/queue methods against host codec stubs."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def method(source, name):
    start = source.index(name)
    start = source.rfind('\n', 0, start) + 1
    opening = source.index('{', start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


def block(source, marker):
    start = source.index(marker)
    opening = source.index('{', start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


class AudioAbortTests(unittest.TestCase):
    def test_worker_interleavings(self):
        source = (ROOT / 'main/audio/audio_service.cc').read_text()
        methods = '\n'.join(method(source, 'AudioService::' + name + '(') for name in (
            'AbortOutput', 'ResetDecoder', 'FlushAudioQueues', 'PushPacketToDecodeQueue',
            'AudioOutputTask', 'OpusCodecTask', 'OpusCodecLoop',
            'ProcessDecodePacket', 'ProcessEncodeTask'))
        harness = (ROOT / 'tests/audio_abort_harness.cc').read_text()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'test.cc').write_text(harness.replace('// AUDIO_SERVICE_METHODS', methods))
            subprocess.run(['c++', '-std=c++17', '-pthread', '-O1', '-g',
                            '-fsanitize=address,undefined', str(path / 'test.cc'), '-o', str(path / 'test')],
                           check=True, timeout=60)
            subprocess.run([str(path / 'test')], check=True, timeout=30)

    def test_blocked_push_callback_reenqueues_after_next_turn_start(self):
        """Protocol+Application+AudioService integration: HandleAudio blocks the
        TAI receive thread inside PushPacketToDecodeQueue(wait=true) on a full
        decode queue while holding ctrl_mutex_. The main loop then aborts the
        turn (CancelTtsLocked -> AbortOutput) and starts the next turn
        (ResetDecoder + aborted_ = false), which releases the push with a
        generation-mismatch false. HandleAudio then invokes the in-flight
        on_incoming_audio_ callback, which re-reads Application state: the
        protocol's in-flight packet belongs to the cancelled turn, but
        Application already cleared aborted_ for the new turn, so the stale
        packet is pushed into the new generation's decode queue. Regression:
        the cancelled callback must not enqueue after its push was invalidated.
        """
        app_source = (ROOT / 'main/application.cc').read_text()
        audio_source = (ROOT / 'main/audio/audio_service.cc').read_text()
        protocol_source = (ROOT / 'main/protocols/tuya_protocol.cc').read_text()
        json_block = block(app_source, 'protocol_->OnIncomingJson(') + ');'
        audio_block = block(app_source, 'protocol_->OnIncomingAudio(') + ');'
        methods = '\n'.join(method(audio_source, 'AudioService::' + name + '(')
                            for name in ('AbortOutput', 'ResetDecoder', 'PushPacketToDecodeQueue'))
        harness = (ROOT / 'tests/audio_integration_harness.cc').read_text()
        harness = (harness
                   .replace('// APPLICATION_CALLBACKS', json_block + '\n' + audio_block)
                   .replace('// AUDIO_SERVICE_METHODS', methods)
                   .replace('// PROTOCOL_METHODS',
                            method(protocol_source, 'TuyaProtocol::HandleAudio(')))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'test.cc').write_text(harness)
            subprocess.run(['c++', '-std=c++17', '-pthread', '-O1', '-g',
                            '-fsanitize=address,undefined', '-DCONFIG_PROTOCOL_TUYA=1',
                            '-DCONFIG_IDF_TARGET_ESP32C3=1', str(path / 'test.cc'),
                            '-o', str(path / 'test')], check=True, timeout=60)
            subprocess.run([str(path / 'test')], check=True, timeout=30)
