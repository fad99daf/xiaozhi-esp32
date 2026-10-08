"""Exercise the real worker's completion boundary without HTTP or FreeRTOS."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def method(source, name):
    start = source.rfind("\n", 0, source.index(name)) + 1
    opening = source.index("{", start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


class MusicAutoNextHostTest(unittest.TestCase):
    def test_natural_playlist_completion_notifies_after_output_drain(self):
        source = (ROOT / "main/audio/music_player.cc").read_text()
        methods = "\n".join(method(source, name) for name in (
            "MusicPlayer::TaskLoop(", "MusicPlayer::IsCancelled(",
            "MusicPlayer::ConsumeAutoNext(", "MusicPlayer::CanPublishAutoNext(", "MusicPlayer::ExpireAutoNext(",
            "MusicPlayer::CancelAutoNext(", "MusicPlayer::BeginTurn(", "MusicPlayer::Stop("))
        transport = (ROOT / "main/protocols/tuya_protocol.cc").read_text()
        start = transport.index("        std::string music_request;", transport.index("void TuyaProtocol::MqttPumpLoop("))
        end = transport.index("        if (rc != OPRT_OK)", start)
        methods += "\nvoid TuyaProtocol::PublishPending() {\nint rc = OPRT_OK; void* client = nullptr;\n" + transport[start:end] + "\n}"
        harness = (ROOT / "tests/music_auto_next_harness.cc").read_text()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "test.cc").write_text(harness.replace("// WORKER_METHODS", methods))
            subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-I", str(ROOT / "main/audio"), str(path / "test.cc"),
                            "-o", str(path / "test")], check=True)
            result = subprocess.run([str(path / "test")], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
