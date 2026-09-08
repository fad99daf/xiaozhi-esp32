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


class AudioAbortTests(unittest.TestCase):
    def test_worker_interleavings(self):
        source = (ROOT / 'main/audio/audio_service.cc').read_text()
        methods = '\n'.join(method(source, 'AudioService::' + name + '(') for name in (
            'AbortOutput', 'ResetDecoder', 'FlushAudioQueues', 'PushPacketToDecodeQueue',
            'AudioOutputTask', 'OpusCodecTask'))
        harness = (ROOT / 'tests/audio_abort_harness.cc').read_text()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'test.cc').write_text(harness.replace('// AUDIO_SERVICE_METHODS', methods))
            subprocess.run(['c++', '-std=c++17', '-pthread', '-O1', '-g',
                            '-fsanitize=address,undefined', str(path / 'test.cc'), '-o', str(path / 'test')],
                           check=True, timeout=60)
            subprocess.run([str(path / 'test')], check=True, timeout=30)
