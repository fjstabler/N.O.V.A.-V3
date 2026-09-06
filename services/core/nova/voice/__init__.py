"""Voice stack: capture, wake word, endpointing, transcription, synthesis.

Capture and playback come from local sound hardware by default and from a
paired device over the bridge when one attaches — see :mod:`nova.voice.remote`.
Everything between the two ends is the same either way.
"""

from .audio import (
    FRAME_BYTES,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    AudioDevice,
    AudioInput,
    AudioOutput,
    MicrophoneSource,
    list_devices,
)
from .remote import RemoteMicrophone, RemoteSpeaker
from .service import ListenState, VoiceService
from .stt import Transcriber, Transcript
from .tts import Synthesiser, normalise_for_speech
from .vad import Endpointer, EndpointState
from .wake import WakeWordDetector

__all__ = [
    "FRAME_BYTES",
    "FRAME_SAMPLES",
    "SAMPLE_RATE",
    "AudioDevice",
    "AudioInput",
    "AudioOutput",
    "EndpointState",
    "Endpointer",
    "ListenState",
    "MicrophoneSource",
    "RemoteMicrophone",
    "RemoteSpeaker",
    "Synthesiser",
    "Transcriber",
    "Transcript",
    "VoiceService",
    "WakeWordDetector",
    "list_devices",
    "normalise_for_speech",
]
