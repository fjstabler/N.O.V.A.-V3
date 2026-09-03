package com.nova.panel

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.AutomaticGainControl
import android.media.audiofx.NoiseSuppressor
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Base64
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import org.json.JSONObject

/**
 * This panel's microphone and speaker, lent to the core.
 *
 * The native counterpart to the browser's `RemoteAudio`, and the reason the
 * panel does not simply use the page's own microphone: `getUserMedia` requires
 * a secure origin, and the core serves plain HTTP on a private address. Rather
 * than putting a certificate on a home server to satisfy a rule that is not
 * protecting anything here, audio is captured natively and the WebView is left
 * to be a display.
 *
 * The second reason is that this has to keep working when the screen is off
 * and the WebView is throttled. A foreground service is the only place on
 * Android where "keep listening" is a promise the system honours.
 *
 * Wake word, endpointing, transcription and synthesis all stay on the core.
 * What runs here is a microphone, a speaker, and the wire between them.
 */
class AudioService : Service(), BridgeSocket.Events {

    private lateinit var prefs: Prefs
    private var bridge: BridgeSocket? = null
    private var recorder: AudioRecord? = null
    private var captureThread: Thread? = null
    private var wakeLock: PowerManager.WakeLock? = null

    private var echoCanceller: AcousticEchoCanceler? = null
    private var noiseSuppressor: NoiseSuppressor? = null
    private var gainControl: AutomaticGainControl? = null

    @Volatile private var sessionId = ""
    @Volatile private var attachId: String? = null
    @Volatile private var capturing = true
    @Volatile private var running = false

    private val playback = Playback()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (running) return START_STICKY
        if (!prefs.configured || !prefs.microphone) {
            stopSelf()
            return START_NOT_STICKY
        }
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) {
            Log.w(TAG, "no microphone permission; not starting")
            stopSelf()
            return START_NOT_STICKY
        }

        startForeground(NOTIFICATION_ID, buildNotification())
        acquireWakeLock()
        running = true
        bridge = BridgeSocket(prefs.socketUrl(), this).also { it.connect() }
        // START_STICKY so the system brings this back if it is ever killed:
        // a panel that quietly stopped listening is worse than one that is
        // obviously off.
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        stopCapture()
        playback.release()
        bridge?.send(TOPIC_DETACH)
        bridge?.close()
        bridge = null
        wakeLock?.takeIf { it.isHeld }?.release()
        wakeLock = null
        super.onDestroy()
    }

    // ------------------------------------------------------------- the bridge

    override fun onOpen() {
        // Attach on every open, not just the first: after a core restart the
        // session it knew about is gone, and frames sent under the old one are
        // refused rather than silently ignored.
        sessionId = ""
        attachId = bridge?.request(TOPIC_ATTACH)
    }

    override fun onResponse(id: String, payload: JSONObject) {
        if (id != attachId) return
        val session = payload.optString("sessionId")
        if (session.isEmpty()) {
            Log.e(TAG, "attach refused: ${payload.optString("message")}")
            return
        }
        sessionId = session
        capturing = true
        Log.i(TAG, "attached as $session at ${payload.optInt("sampleRate")} Hz")
        startCapture()
    }

    override fun onEvent(topic: String, payload: JSONObject) {
        // Every panel on the tailnet sees every event; only the one holding the
        // session should act on the audio ones.
        val session = payload.optString("sessionId")
        if (session.isNotEmpty() && session != sessionId) return
        when (topic) {
            TOPIC_PLAY -> playback.play(
                payload.optString("wav"),
                payload.optInt("sampleRate", 24000),
            )
            TOPIC_STOP -> playback.stop()
            TOPIC_CAPTURE -> capturing = payload.optBoolean("capture", true)
        }
    }

    override fun onClosed(authorised: Boolean) {
        sessionId = ""
        if (!authorised) {
            // The token is wrong and will stay wrong. Stop rather than hammer.
            Log.e(TAG, "stopping: the core refused this panel's token")
            stopSelf()
        }
    }

    // ---------------------------------------------------------------- capture

    private fun startCapture() {
        if (captureThread != null) return
        val minBuffer = AudioRecord.getMinBufferSize(SAMPLE_RATE, IN_CHANNEL, ENCODING)
        if (minBuffer <= 0) {
            Log.e(TAG, "this device cannot record 16 kHz mono")
            return
        }
        // Several frames of headroom, so a scheduling hiccup drops nothing.
        val bufferSize = maxOf(minBuffer, FRAME_BYTES * 8)

        val record = try {
            AudioRecord(
                // VOICE_RECOGNITION rather than MIC: it is the source Android
                // tunes for speech, and on most devices it is the one that
                // leaves the platform's own AEC and noise suppression in play.
                MediaRecorder.AudioSource.VOICE_RECOGNITION,
                SAMPLE_RATE, IN_CHANNEL, ENCODING, bufferSize,
            )
        } catch (e: SecurityException) {
            Log.e(TAG, "microphone permission revoked", e)
            return
        }
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            Log.e(TAG, "could not open the microphone")
            record.release()
            return
        }

        attachEffects(record.audioSessionId)
        record.startRecording()
        recorder = record

        captureThread = Thread({ captureLoop(record) }, "nova-capture").apply {
            priority = Thread.MAX_PRIORITY
            start()
        }
    }

    /**
     * Read the microphone forever and post whole frames to the core.
     *
     * Reads are exactly one wake-word frame, so nothing has to be re-cut on
     * either side in the ordinary case — the core will re-chunk anyway, but a
     * frame that arrives whole is one that cannot be split across a stall.
     */
    private fun captureLoop(record: AudioRecord) {
        val frame = ByteArray(FRAME_BYTES)
        while (running && !Thread.currentThread().isInterrupted) {
            var filled = 0
            while (filled < FRAME_BYTES) {
                val read = record.read(frame, filled, FRAME_BYTES - filled)
                if (read <= 0) {
                    if (read == AudioRecord.ERROR_INVALID_OPERATION || read == AudioRecord.ERROR_BAD_VALUE) {
                        Log.e(TAG, "microphone read failed: $read")
                        return
                    }
                    break
                }
                filled += read
            }
            if (filled < FRAME_BYTES) continue

            // Dropped rather than queued: this is live audio, and a frame from
            // while N.O.V.A. was speaking is not worth hearing late.
            if (!capturing || sessionId.isEmpty()) continue

            val payload = JSONObject()
                .put("sessionId", sessionId)
                .put("pcm", Base64.encodeToString(frame, Base64.NO_WRAP))
            bridge?.send(TOPIC_FRAME, payload)
        }
    }

    private fun stopCapture() {
        captureThread?.interrupt()
        captureThread = null
        recorder?.runCatching {
            if (recordingState == AudioRecord.RECORDSTATE_RECORDING) stop()
            release()
        }
        recorder = null
        echoCanceller?.release(); echoCanceller = null
        noiseSuppressor?.release(); noiseSuppressor = null
        gainControl?.release(); gainControl = null
    }

    /**
     * Turn on whatever the hardware offers.
     *
     * On a panel the speaker is inches from the microphone, so without echo
     * cancellation N.O.V.A.'s own voice is loud enough to trigger the wake
     * word it is currently speaking through. The core mutes capture while it
     * talks, which covers most of it; this covers the overlap at each end.
     */
    private fun attachEffects(audioSessionId: Int) {
        runCatching {
            if (AcousticEchoCanceler.isAvailable()) {
                echoCanceller = AcousticEchoCanceler.create(audioSessionId)?.apply { enabled = true }
            }
        }.onFailure { Log.w(TAG, "no echo canceller", it) }
        runCatching {
            if (NoiseSuppressor.isAvailable()) {
                noiseSuppressor = NoiseSuppressor.create(audioSessionId)?.apply { enabled = true }
            }
        }.onFailure { Log.w(TAG, "no noise suppressor", it) }
        runCatching {
            if (AutomaticGainControl.isAvailable()) {
                gainControl = AutomaticGainControl.create(audioSessionId)?.apply { enabled = true }
            }
        }.onFailure { Log.w(TAG, "no gain control", it) }
    }

    // --------------------------------------------------------------- notifying

    private fun createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.audio_channel_name),
            NotificationManager.IMPORTANCE_LOW,
        ).apply { setShowBadge(false) }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    private fun buildNotification(): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, PanelActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.audio_notification_title))
            .setContentText(getString(R.string.audio_notification_body))
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setOngoing(true)
            .setContentIntent(open)
            .build()
    }

    private fun acquireWakeLock() {
        val power = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "nova:panel-audio").apply {
            setReferenceCounted(false)
            acquire(WAKE_LOCK_TIMEOUT_MS)
        }
    }

    // --------------------------------------------------------------- playback

    /**
     * Plays the clips the core sends back.
     *
     * `AudioTrack` rather than `MediaPlayer` because a barge-in has to land
     * mid-sentence: this writes PCM in a loop it can simply abandon, where a
     * MediaPlayer would have to be torn down and rebuilt for every reply.
     */
    private inner class Playback {
        private var track: AudioTrack? = null
        @Volatile private var generation = 0
        private var worker: Thread? = null

        fun play(wavBase64: String, fallbackRate: Int) {
            if (wavBase64.isEmpty()) return
            val bytes = runCatching { Base64.decode(wavBase64, Base64.DEFAULT) }.getOrNull() ?: return
            val clip = Wav.parse(bytes, fallbackRate) ?: return

            stop()
            val mine = ++generation
            worker = Thread({ write(clip, mine) }, "nova-playback").apply { start() }
        }

        private fun write(clip: Wav.Clip, mine: Int) {
            val minBuffer = AudioTrack.getMinBufferSize(clip.sampleRate, OUT_CHANNEL, ENCODING)
            if (minBuffer <= 0) return
            val output = AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ASSISTANT)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build()
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setEncoding(ENCODING)
                        .setSampleRate(clip.sampleRate)
                        .setChannelMask(OUT_CHANNEL)
                        .build()
                )
                .setBufferSizeInBytes(maxOf(minBuffer, clip.pcm.size.coerceAtMost(minBuffer * 4)))
                .setTransferMode(AudioTrack.MODE_STREAM)
                .build()
            track = output
            output.play()

            var offset = 0
            val chunk = 4096
            while (offset < clip.pcm.size && generation == mine) {
                val size = minOf(chunk, clip.pcm.size - offset)
                val written = output.write(clip.pcm, offset, size)
                if (written <= 0) break
                offset += written
            }
            if (generation == mine) {
                // Let the buffer drain rather than cutting the last syllable.
                runCatching { output.stop() }
            }
            runCatching { output.release() }
            if (track === output) track = null
        }

        fun stop() {
            generation += 1
            runCatching {
                track?.pause()
                track?.flush()
            }
            worker?.interrupt()
            worker = null
        }

        fun release() {
            stop()
            runCatching { track?.release() }
            track = null
        }
    }

    companion object {
        private const val TAG = "NovaAudio"

        /** What the core's wake detector and Whisper both expect. */
        const val SAMPLE_RATE = 16000
        /** 80 ms at 16 kHz — one openWakeWord frame. */
        const val FRAME_SAMPLES = 1280
        const val FRAME_BYTES = FRAME_SAMPLES * 2

        private const val IN_CHANNEL = AudioFormat.CHANNEL_IN_MONO
        private const val OUT_CHANNEL = AudioFormat.CHANNEL_OUT_MONO
        private const val ENCODING = AudioFormat.ENCODING_PCM_16BIT

        private const val CHANNEL_ID = "nova-audio"
        private const val NOTIFICATION_ID = 1
        // Renewed while the service lives; a bounded lock cannot strand the
        // device awake if this is ever killed without onDestroy running.
        private const val WAKE_LOCK_TIMEOUT_MS = 24L * 60 * 60 * 1000

        private const val TOPIC_ATTACH = "audio.source.attach"
        private const val TOPIC_DETACH = "audio.source.detach"
        private const val TOPIC_FRAME = "audio.source.frame"
        private const val TOPIC_PLAY = "voice.remote.play"
        private const val TOPIC_STOP = "voice.remote.stop"
        private const val TOPIC_CAPTURE = "voice.remote.capture"

        private const val RIFF = 0x46464952 // "RIFF" little-endian
        private const val WAVE = 0x45564157 // "WAVE"
        private const val FMT = 0x20746d66 // "fmt "
        private const val DATA = 0x61746164 // "data"

        fun start(context: Context) {
            val intent = Intent(context, AudioService::class.java)
            ContextCompat.startForegroundService(context, intent)
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, AudioService::class.java))
        }
    }
}
