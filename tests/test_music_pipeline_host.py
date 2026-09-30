"""Compile and run the transport and TTS timing rules without ESP-IDF hardware."""

from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MusicPipelineHostTest(unittest.TestCase):
    def test_tai_text_and_pre_tts_gate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = Path(temp_dir) / "music_pipeline_test"
            build = subprocess.run([
                "c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                "-I", str(ROOT / "main/protocols"),
                "-I", str(ROOT / "main/audio"),
                str(ROOT / "tests/music_pipeline_test.cc"),
                str(ROOT / "main/protocols/tuya_text_stream.cc"),
                "-o", str(binary),
            ], capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            subprocess.run([str(binary)], check=True, capture_output=True, text=True)
