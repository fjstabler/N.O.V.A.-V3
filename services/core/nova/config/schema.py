"""Typed configuration model.

One pydantic tree describes everything the settings panel can change. The UI is
generated from this schema's field metadata rather than hand-maintained, so a
new setting appears in the panel the moment it is declared here.

Secrets are marked with ``json_schema_extra={"secret": True}``; the transport
layer redacts those before anything leaves the process.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

Secret = Field(default="", json_schema_extra={"secret": True})

AnimationQuality = Literal["low", "balanced", "high", "ultra"]
ThemeName = Literal["nova-blue", "arc-cyan", "ember", "monochrome", "aurora"]
WhisperSize = Literal["tiny", "base", "small", "medium", "large-v3", "distil-large-v3"]
ComputeDevice = Literal["auto", "cuda", "cpu"]


class OpenAISettings(BaseModel):
    """The single permitted cloud dependency: reasoning only."""

    api_key: str = Secret
    model: str = Field(default="gpt-4o", description="Reasoning model")
    vision_model: str = Field(default="gpt-4o", description="Model used for image understanding")
    base_url: str = Field(default="", description="Override for OpenAI-compatible endpoints")
    temperature: float = Field(default=0.6, ge=0.0, le=2.0)
    max_output_tokens: int = Field(default=1024, ge=64, le=16384)
    request_timeout: float = Field(default=60.0, ge=5.0, le=300.0)
    max_tool_iterations: int = Field(default=6, ge=1, le=20)

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip())


class WakeWordSettings(BaseModel):
    enabled: bool = True
    phrase: str = Field(default="hey nova", description="Spoken activation phrase")
    model: str = Field(default="hey_nova", description="openWakeWord model name or .onnx path")
    sensitivity: float = Field(
        default=0.55, ge=0.05, le=0.95, description="Higher = easier to trigger"
    )
    cooldown_seconds: float = Field(default=1.5, ge=0.0, le=10.0)
    #: Suppresses self-triggering while N.O.V.A. is speaking.
    mute_while_speaking: bool = True


class SpeechToTextSettings(BaseModel):
    model_size: WhisperSize = "base"
    device: ComputeDevice = "auto"
    compute_type: str = Field(default="auto", description="ct2 compute type, e.g. float16 / int8")
    language: str = Field(default="en", description="ISO code, or 'auto' to detect")
    beam_size: int = Field(default=1, ge=1, le=10)
    #: Endpointing — how much trailing silence closes an utterance.
    silence_ms: int = Field(default=800, ge=200, le=4000)
    max_utterance_seconds: float = Field(default=30.0, ge=3.0, le=120.0)
    vad_aggressiveness: int = Field(default=2, ge=0, le=3)


class TextToSpeechSettings(BaseModel):
    enabled: bool = True
    voice: str = Field(default="af_sarah", description="Kokoro voice id")
    speed: float = Field(default=1.05, ge=0.5, le=2.0)
    volume: float = Field(default=1.0, ge=0.0, le=1.0)
    #: Speaks each sentence as it is produced instead of waiting for the full reply.
    stream_sentences: bool = True


class AudioSettings(BaseModel):
    input_device: str = Field(
        default="", description="Substring match on device name; empty = default"
    )
    output_device: str = Field(default="")
    input_gain: float = Field(default=1.0, ge=0.1, le=4.0)
    sample_rate: int = Field(default=16000)


class VoiceSettings(BaseModel):
    enabled: bool = True
    wake: WakeWordSettings = Field(default_factory=WakeWordSettings)
    stt: SpeechToTextSettings = Field(default_factory=SpeechToTextSettings)
    tts: TextToSpeechSettings = Field(default_factory=TextToSpeechSettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    #: Keep listening for a follow-up after N.O.V.A. finishes speaking.
    conversation_mode: bool = True
    follow_up_window: float = Field(default=6.0, ge=0.0, le=30.0)


class AppearanceSettings(BaseModel):
    theme: ThemeName = "nova-blue"
    animation_quality: AnimationQuality = "high"
    gpu_acceleration: bool = True
    clock_24_hour: bool = True
    #: The corner readout is designed around a visible seconds tick.
    show_seconds: bool = True
    show_date: bool = True
    core_scale: float = Field(default=1.0, ge=0.5, le=1.6)
    bloom_intensity: float = Field(default=1.0, ge=0.0, le=2.0)
    particle_density: float = Field(default=1.0, ge=0.0, le=2.0)
    reduce_motion: bool = False


class MemorySettings(BaseModel):
    enabled: bool = True
    #: Semantic recall. Falls back to SQLite FTS5 when embeddings are unavailable.
    semantic_recall: bool = True
    embedding_model: str = Field(default="sentence-transformers/all-MiniLM-L6-v2")
    max_recalled: int = Field(default=8, ge=0, le=40)
    conversation_turns: int = Field(
        default=12, ge=0, le=60, description="Turns kept in working context"
    )
    retention_days: int = Field(default=0, ge=0, description="0 = keep forever")


class NotificationSettings(BaseModel):
    enabled: bool = True
    position: Literal["top-right", "top-left", "bottom-right", "bottom-left", "top-center"] = (
        "top-right"
    )
    default_timeout: float = Field(default=8.0, ge=1.0, le=60.0)
    max_visible: int = Field(default=4, ge=1, le=10)
    #: Never interrupt a conversation — queued until N.O.V.A. returns to idle.
    defer_while_busy: bool = True
    speak_critical: bool = True
    do_not_disturb: bool = False
    quiet_hours_start: str = Field(default="", description="HH:MM, empty to disable")
    quiet_hours_end: str = Field(default="")


class HomeAssistantSettings(BaseModel):
    enabled: bool = False
    url: str = Field(default="http://homeassistant.local:8123")
    token: str = Secret
    verify_ssl: bool = True
    #: Entity domains exposed to the assistant.
    exposed_domains: list[str] = Field(
        default_factory=lambda: [
            "light",
            "switch",
            "climate",
            "cover",
            "scene",
            "script",
            "media_player",
            "fan",
            "lock",
            "binary_sensor",
            "sensor",
            "person",
            "camera",
        ]
    )


class MQTTSettings(BaseModel):
    enabled: bool = False
    host: str = "localhost"
    port: int = Field(default=1883, ge=1, le=65535)
    username: str = ""
    password: str = Secret
    tls: bool = False
    client_id: str = "nova-core"
    base_topic: str = "nova"
    subscribe: list[str] = Field(default_factory=lambda: ["nova/#"])


class CalendarAccount(BaseModel):
    name: str = "personal"
    url: str = ""
    username: str = ""
    password: str = Secret
    read_only: bool = False


class CalendarSettings(BaseModel):
    enabled: bool = False
    accounts: list[CalendarAccount] = Field(default_factory=list)
    default_account: str = "personal"
    sync_interval_minutes: int = Field(default=15, ge=1, le=240)
    reminder_lead_minutes: int = Field(default=10, ge=0, le=1440)
    week_starts_monday: bool = True


class ServerSettings(BaseModel):
    """Ubuntu server administration."""

    enabled: bool = True
    monitor_interval_seconds: float = Field(default=3.0, ge=0.5, le=60.0)
    docker_enabled: bool = True
    docker_socket: str = Field(default="unix:///var/run/docker.sock")
    systemd_enabled: bool = True
    #: Services the assistant may start/stop/restart without confirmation.
    managed_units: list[str] = Field(default_factory=list)
    #: Shell execution is opt-in, and even then goes through the policy engine.
    allow_shell: bool = False
    shell_timeout_seconds: float = Field(default=30.0, ge=1.0, le=600.0)
    #: Extra binaries the policy engine treats as read-only-safe.
    extra_readonly_commands: list[str] = Field(default_factory=list)
    #: Filesystem roots the assistant may read/write.
    file_roots: list[str] = Field(default_factory=list)
    alert_cpu_percent: float = Field(default=92.0, ge=50.0, le=100.0)
    alert_memory_percent: float = Field(default=92.0, ge=50.0, le=100.0)
    alert_disk_percent: float = Field(default=90.0, ge=50.0, le=100.0)
    alert_gpu_temp_c: float = Field(default=83.0, ge=50.0, le=110.0)


class HomeLabService(BaseModel):
    """One self-hosted service N.O.V.A. should understand."""

    kind: Literal[
        "homepage",
        "adguard",
        "uptime-kuma",
        "node-red",
        "jellyfin",
        "plex",
        "immich",
        "portainer",
        "generic",
    ] = "generic"
    name: str = ""
    url: str = ""
    api_key: str = Secret
    username: str = ""
    password: str = Secret
    enabled: bool = True
    verify_ssl: bool = True

    @field_validator("name")
    @classmethod
    def _default_name(cls, v: str) -> str:
        return v.strip()


class HomeLabSettings(BaseModel):
    enabled: bool = False
    services: list[HomeLabService] = Field(default_factory=list)
    poll_interval_seconds: float = Field(default=60.0, ge=10.0, le=900.0)


class NamedCamera(BaseModel):
    """A local camera device N.O.V.A. can show a live view of by name.

    Separate from `camera_index`, which is the one camera `look_at_camera`
    describes by default — this is a list, so a machine with more than one
    attached camera (a laptop's built-in one plus a USB webcam, say) can give
    each a name to show by, the same way a Home Assistant camera entity has
    a friendly name.
    """

    name: str = ""
    index: int = 0

    @field_validator("name")
    @classmethod
    def _default_name(cls, v: str) -> str:
        return v.strip()


class VisionSettings(BaseModel):
    enabled: bool = False
    screen_capture: bool = True
    camera_enabled: bool = False
    camera_index: int = 0
    named_cameras: list[NamedCamera] = Field(default_factory=list)
    max_image_edge: int = Field(default=1280, ge=256, le=4096)
    jpeg_quality: int = Field(default=82, ge=40, le=100)


class SecuritySettings(BaseModel):
    """Room-watch: alert when an unrecognised face is seen on a named camera.

    Armed and disarmed by voice ("watch my room" / "stand down") rather than
    left running continuously — partly because that is what was asked for,
    partly because the camera's own indicator light stays lit the whole time
    it is armed, which should be a deliberate choice each time, not a
    permanent background state.

    Deliberately no separate `enabled` toggle: arming needs a configured
    camera and at least one enrolled face regardless, and every arm resets
    to disarmed on restart — those already gate everything that matters,
    so a fourth switch would only be one more silent way for the documented
    setup steps to do nothing with no indication why.
    """

    #: Matches an entry in vision.named_cameras by name.
    camera_name: str = ""
    poll_interval_seconds: float = Field(default=1.5, ge=0.5, le=10.0)
    #: Consecutive unrecognised-face detections required before alerting —
    #: one odd frame (motion blur, a bad angle) should not be enough on its own.
    confirm_frames: int = Field(default=2, ge=1, le=10)
    #: Minimum time between alerts, so one visit does not fire repeatedly
    #: while the person is still in frame.
    alert_cooldown_seconds: float = Field(default=120.0, ge=10.0, le=3600.0)
    #: SFace's own documented cosine-similarity threshold for "same person".
    match_threshold: float = Field(default=0.363, ge=0.0, le=1.0)
    alert_message: str = "You should not be in here. Fin has been alerted."
    #: A free push-notification relay (ntfy.sh by default; self-hostable).
    ntfy_server: str = Field(default="https://ntfy.sh")
    #: Generated on first arm if left blank — see SecurityService._ensure_topic.
    ntfy_topic: str = ""


class PluginSettings(BaseModel):
    #: Skills disabled by name. Everything discovered is enabled by default.
    disabled: list[str] = Field(default_factory=list)
    #: Extra directories scanned for third-party skill packages.
    search_paths: list[str] = Field(default_factory=list)
    allow_third_party: bool = True


class DeveloperSettings(BaseModel):
    debug_mode: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    json_logs: bool = False
    show_fps: bool = False
    show_state_badge: bool = False
    #: Enables the hidden text console (Ctrl+Shift+K) for testing without a mic.
    text_console: bool = True
    trace_tool_calls: bool = False


class TransportSettings(BaseModel):
    host: str = Field(
        default="127.0.0.1",
        description="Loopback by default. A Tailscale address reaches other devices on your "
        "tailnet (e.g. the mobile web client); anything else needs a real reason.",
    )
    port: int = Field(default=8765, ge=1024, le=65535)
    #: Shared secret the UI presents on connect; generated on first run.
    token: str = Secret
    public_url: str = Field(
        default="",
        description="The HTTPS address the mobile web client is actually reachable at "
        "(e.g. https://your-machine.your-tailnet.ts.net) — the reverse proxy's address, "
        "not host:port above, since a proxy normally terminates TLS on a different port. "
        "Used to build a link straight to a camera in a push notification; left blank, "
        "an alert still speaks, notifies and pushes, just without a tap-through link.",
    )


class AssistantSettings(BaseModel):
    name: str = "NOVA"
    wake_response: str = Field(default="", description="Optional spoken acknowledgement")
    persona: str = Field(
        default=(
            "You are N.O.V.A., a calm, precise, and highly capable assistant that runs locally "
            "on the user's machines. You speak in short, confident sentences. You do not pad "
            "answers with pleasantries or restate the question. When you act on the user's "
            "systems you say what you did, briefly."
        ),
        description="System persona used for reasoning",
    )
    units: Literal["metric", "imperial"] = "metric"
    timezone: str = Field(default="", description="IANA name; empty = system timezone")
    location: str = Field(default="", description="City used for weather and sunrise")


class NovaSettings(BaseModel):
    """Root settings document persisted to ``~/.config/nova/config.toml``."""

    version: int = 1
    assistant: AssistantSettings = Field(default_factory=AssistantSettings)
    openai: OpenAISettings = Field(default_factory=OpenAISettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    appearance: AppearanceSettings = Field(default_factory=AppearanceSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    home_assistant: HomeAssistantSettings = Field(default_factory=HomeAssistantSettings)
    mqtt: MQTTSettings = Field(default_factory=MQTTSettings)
    calendar: CalendarSettings = Field(default_factory=CalendarSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    homelab: HomeLabSettings = Field(default_factory=HomeLabSettings)
    vision: VisionSettings = Field(default_factory=VisionSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    plugins: PluginSettings = Field(default_factory=PluginSettings)
    developer: DeveloperSettings = Field(default_factory=DeveloperSettings)
    transport: TransportSettings = Field(default_factory=TransportSettings)


#: Sentinel the UI receives in place of a stored secret, and may send back
#: unchanged to mean "leave it alone".
REDACTED = "••••••••"

SettingsPath = Annotated[str, "dot.delimited.path"]
