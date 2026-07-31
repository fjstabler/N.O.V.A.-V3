"""Local voice stack: capture, wake word, endpointing, transcription, synthesis."""

from .audio import SAMPLE_RATE, AudioDevice, AudioInput, AudioOutput, list_devices
from .service import ListenState, VoiceService
from .stt import Transcriber, Transcript
from .tts import Synthesiser, normalise_for_speech
from .vad import Endpointer, EndpointState
from .wake import WakeWordDetector

__all__ = [
    "SAMPLE_RATE",
    "AudioDevice",
    "AudioInput",
    "AudioOutput",
    "EndpointState",
    "Endpointer",
    "ListenState",
    "Synthesiser",
    "Transcriber",
    "Transcript",
    "VoiceService",
    "WakeWordDetector",
    "list_devices",
    "normalise_for_speech",
]
