"""N.O.V.A. over the telephone.

A phone call is the same assistant with three differences, and each one is a
deliberate departure from how the panel works:

* **No wake word.** The call *is* the attention signal. Once it connects the
  microphone is open until somebody hangs up.
* **The conversation runs to its natural end** rather than a six-second
  follow-up window, and either side can interrupt the other.
* **It can start the conversation.** Every other surface waits to be spoken
  to; this one dials, and says why it is calling before anything else.

The audio is the awkward part. Telephony is 8 kHz G.711 μ-law, an encoding
designed in 1972 to fit a voice into 64 kbit/s; Whisper and Kokoro want 16 kHz
linear PCM. `audio` converts between them in both directions, statefully, so
the twenty-millisecond packets a phone line arrives in do not click at every
boundary.
"""

from __future__ import annotations

from .audio import Downsampler, Upsampler, mulaw_to_pcm16, pcm16_to_mulaw

__all__ = [
    "Downsampler",
    "Upsampler",
    "mulaw_to_pcm16",
    "pcm16_to_mulaw",
]
