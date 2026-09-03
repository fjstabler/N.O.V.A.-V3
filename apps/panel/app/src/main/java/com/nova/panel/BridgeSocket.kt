package com.nova.panel

import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.UUID
import java.util.concurrent.TimeUnit
import kotlin.math.min
import kotlin.math.pow

/**
 * The panel's own connection to the core, separate from the WebView's.
 *
 * Two sockets to one core looks redundant until the alternative is tried: the
 * page would have to relay every microphone frame through a JavaScript bridge,
 * on the UI thread, at eight messages a second, and would stop doing it the
 * moment the WebView was throttled or reloaded. Audio is the one thing on this
 * device that must not pause, so it gets a connection that belongs to a
 * foreground service rather than to a document.
 *
 * Reconnection is the same shape as the web client's: exponential backoff with
 * full jitter, because a core that restarts should not be met by every panel
 * in the house at once.
 */
class BridgeSocket(
    private val url: String,
    private val listener: Events,
) : WebSocketListener() {

    interface Events {
        /** The socket is up; the caller should (re-)attach whatever it owns. */
        fun onOpen()
        fun onEvent(topic: String, payload: JSONObject)
        fun onResponse(id: String, payload: JSONObject)
        fun onClosed(authorised: Boolean)
    }

    private val client = OkHttpClient.Builder()
        // The core pings every 20 s; anything much longer than that and a dead
        // link would go unnoticed until someone spoke into it.
        .pingInterval(20, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    @Volatile private var socket: WebSocket? = null
    @Volatile private var closedByUs = false
    private var attempt = 0

    fun connect() {
        closedByUs = false
        socket = client.newWebSocket(Request.Builder().url(url).build(), this)
    }

    fun close() {
        closedByUs = true
        socket?.close(1000, "panel stopping")
        socket = null
    }

    /**
     * Send a request and ignore the reply.
     *
     * Everything this app sends is either a stream (microphone frames, where a
     * per-frame promise would be pure overhead) or a fire-once instruction
     * whose effect arrives as an event anyway.
     */
    fun send(topic: String, payload: JSONObject = JSONObject()): Boolean {
        val envelope = JSONObject()
            .put("v", PROTOCOL_VERSION)
            .put("kind", "request")
            .put("topic", topic)
            .put("id", UUID.randomUUID().toString().replace("-", ""))
            .put("ts", System.currentTimeMillis() / 1000.0)
            .put("payload", payload)
        return socket?.send(envelope.toString()) ?: false
    }

    /** Send a request whose reply the caller wants, correlated by the returned id. */
    fun request(topic: String, payload: JSONObject = JSONObject()): String? {
        val id = UUID.randomUUID().toString().replace("-", "")
        val envelope = JSONObject()
            .put("v", PROTOCOL_VERSION)
            .put("kind", "request")
            .put("topic", topic)
            .put("id", id)
            .put("ts", System.currentTimeMillis() / 1000.0)
            .put("payload", payload)
        return if (socket?.send(envelope.toString()) == true) id else null
    }

    // ------------------------------------------------------------- callbacks

    override fun onOpen(webSocket: WebSocket, response: Response) {
        attempt = 0
        Log.i(TAG, "connected")
        listener.onOpen()
    }

    override fun onMessage(webSocket: WebSocket, text: String) {
        val envelope = try {
            JSONObject(text)
        } catch (e: Exception) {
            Log.w(TAG, "unparseable message", e)
            return
        }
        if (envelope.optInt("v") != PROTOCOL_VERSION) {
            Log.e(TAG, "protocol mismatch: core speaks v${envelope.optInt("v")}")
            return
        }
        val payload = envelope.optJSONObject("payload") ?: JSONObject()
        when (envelope.optString("kind")) {
            "event", "hello" -> listener.onEvent(envelope.optString("topic"), payload)
            "response", "error" -> listener.onResponse(envelope.optString("id"), payload)
        }
    }

    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
        socket = null
        if (closedByUs) return
        Log.w(TAG, "socket failed: ${t.message}")
        listener.onClosed(true)
        scheduleReconnect()
    }

    override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
        socket = null
        if (closedByUs) return
        // 4401 is the core's own "unauthorised". Retrying a token it has
        // already refused is pointless; the panel has to be paired again.
        val authorised = code != 4401
        if (!authorised) Log.e(TAG, "token rejected by the core")
        listener.onClosed(authorised)
        if (authorised) scheduleReconnect()
    }

    private fun scheduleReconnect() {
        attempt += 1
        val ceiling = min(MAX_BACKOFF_MS.toDouble(), 400.0 * 2.0.pow(min(attempt, 6))).toLong()
        val delay = (Math.random() * ceiling).toLong()
        Thread {
            try {
                Thread.sleep(delay)
            } catch (e: InterruptedException) {
                return@Thread
            }
            if (!closedByUs) connect()
        }.start()
    }

    companion object {
        private const val TAG = "NovaBridge"
        const val PROTOCOL_VERSION = 1
        private const val MAX_BACKOFF_MS = 15_000
    }
}
