"""Run real music control methods against the stop/resume cards from device logs."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CJSON = ROOT / "components/esp-agentic-kit/agentic-kit/third_party/cJSON"


def method(source, name):
    start = source.rfind("\n", 0, source.index(name)) + 1
    opening = source.index("{", start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


class MusicControlHostTest(unittest.TestCase):
    def test_cloud_stop_preserves_stream_for_resume(self):
        source = (ROOT / "main/audio/music_player.cc").read_text()
        methods = "\n".join(method(source, name) for name in (
            "GetStringField(", "IsHttpUrl(", "SelectSkillContainer(",
            "MusicPlayer::HandleSkillCard(", "MusicPlayer::BeginTurn(",
            "MusicPlayer::NotifyTtsStarted(", "MusicPlayer::NotifyTtsFinished(",
            "MusicPlayer::NotifyTtsAborted(", "MusicPlayer::Stop("))
        harness = (ROOT / "tests/music_control_harness.cc").read_text()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "test.cc").write_text(harness.replace("// MUSIC_CONTROL_METHODS", methods))
            subprocess.run(["cc", "-std=c99", "-I", str(CJSON), "-c",
                            str(CJSON / "cJSON.c"), "-o", str(path / "cJSON.o")], check=True)
            subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra",
                            "-Wno-unused-variable", "-I", str(CJSON),
                            "-I", str(ROOT / "main/audio"), str(path / "test.cc"),
                            str(path / "cJSON.o"), "-o", str(path / "test")], check=True)
            subprocess.run([str(path / "test")], check=True, timeout=10)
