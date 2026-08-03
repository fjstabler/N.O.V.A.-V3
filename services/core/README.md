# nova-core

The N.O.V.A. core service: voice pipeline, reasoning, memory, skills, system
control and home integrations. Runs headless and speaks to the desktop shell
over a local WebSocket.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # runtime, transport, memory, skills
pip install -e ".[voice,ai]"     # add the local voice stack + OpenAI
python -m nova                   # start the service
```

Install only the extras you need — the core boots without any of them and
reports the missing capability rather than failing.

| Extra        | Enables                                              |
| ------------ | ---------------------------------------------------- |
| `voice`      | microphone, transcription and synthesis              |
| `wake`       | wake word — separate, see the note below             |
| `audio` `stt` `tts` | the individual stages, if you want just one    |
| `ai`         | OpenAI reasoning and vision                          |
| `embeddings` | semantic memory recall (falls back to FTS5 without)  |
| `home`       | MQTT and CalDAV calendars                            |
| `server`     | Docker SDK                                           |
| `vision`     | screen and camera capture                            |

`wake` is deliberately not part of `voice`. openWakeWord declares
`tflite-runtime` on Linux, which lags new Python releases badly, and N.O.V.A.
drives it through ONNX and never touches TFLite. Keeping it separate means a
Python too new for TFLite still gets a working microphone and push-to-talk.

See `../../docs/ARCHITECTURE.md` for how the pieces fit together and
`../../docs/PLUGINS.md` for writing a skill.
