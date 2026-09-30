"""Exercise production HTTP/decode loops with a scripted decoder contract.

The proprietary ESP codec runs on the board; these tests cover how the caller
handles network fragments, consumption, output resize, EOF and cancellation.
"""
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_music_control_host import method

ROOT = Path(__file__).resolve().parents[1]


class MusicStreamHostTest(unittest.TestCase):
    def test_stream_decoder_contract(self):
        source = (ROOT / "main/audio/music_player.cc").read_text()
        methods = [method(source, name) for name in
                   ("MusicPlayer::StreamMp3(", "MusicPlayer::DecodeAvailable(")]
        declarations = "\n".join(m.split("{", 1)[0].replace("MusicPlayer::", "")
                                 + ";" for m in methods)
        harness = (ROOT / "tests/music_stream_harness.cc").read_text()
        harness = harness.replace("// DECLARATIONS", declarations)
        harness = harness.replace("// METHODS", "\n".join(methods))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "test.cc").write_text(harness)
            includes = ROOT / "managed_components/espressif__esp_audio_codec/include"
            subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra",
                            "-I", str(includes), "-I", str(includes / "decoder"),
                            str(path / "test.cc"), "-o", str(path / "test")], check=True)
            subprocess.run([str(path / "test")], check=True, timeout=10)
