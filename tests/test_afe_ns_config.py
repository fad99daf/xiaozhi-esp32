"""Regression checks for the conversation AFE configuration."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AFE_PROCESSOR_SOURCE = (
    PROJECT_ROOT / "main/audio/processors/afe_audio_processor.cc"
)


class TestConversationAfeNoiseSuppression(unittest.TestCase):
    def test_conversation_afe_keeps_device_ns_disabled(self):
        """AEC must not be combined with the unstable device-side NS path."""
        source = AFE_PROCESSOR_SOURCE.read_text()
        initialize_start = source.index("void AfeAudioProcessor::Initialize(")
        initialize_end = source.index("AfeAudioProcessor::~AfeAudioProcessor()", initialize_start)
        initialize = source[initialize_start:initialize_end]

        self.assertIn("afe_config->ns_init = false;", initialize)
        self.assertNotIn("ESP_NSNET_PREFIX", initialize)
        self.assertNotIn("afe_config->ns_model_name =", initialize)
        self.assertNotIn("afe_config->afe_ns_mode =", initialize)


if __name__ == "__main__":
    unittest.main()
