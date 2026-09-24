package com.nova.panel

import android.content.Context
import android.content.SharedPreferences

/**
 * Where the panel remembers which core it belongs to.
 *
 * Three facts and a switch. Deliberately not synced, exported or backed up:
 * the token gates the whole bridge, and a panel is paired by someone standing
 * in front of it once.
 */
class Prefs(context: Context) {

    private val store: SharedPreferences =
        context.getSharedPreferences("nova-panel", Context.MODE_PRIVATE)

    var host: String
        get() = store.getString(KEY_HOST, "") ?: ""
        set(value) = store.edit().putString(KEY_HOST, value.trim()).apply()

    var port: Int
        get() = store.getInt(KEY_PORT, DEFAULT_PORT)
        set(value) = store.edit().putInt(KEY_PORT, value).apply()

    var token: String
        get() = store.getString(KEY_TOKEN, "") ?: ""
        set(value) = store.edit().putString(KEY_TOKEN, value.trim()).apply()

    /**
     * Whether this panel offers its microphone and speaker to the core.
     *
     * On by default — a screen on a wall you cannot talk to is a clock. It is
     * still a switch, because a second panel in the same house should not both
     * be trying to be the one microphone.
     */
    var microphone: Boolean
        get() = store.getBoolean(KEY_MIC, true)
        set(value) = store.edit().putBoolean(KEY_MIC, value).apply()

    val configured: Boolean
        get() = host.isNotBlank() && token.isNotBlank()

    /** The page the WebView loads. */
    fun appUrl(): String =
        // `audio=0` because the microphone is handled natively rather than in
        // the WebView: getUserMedia needs a secure origin, and the core serves
        // plain HTTP on a private address. See AudioService.
        "http://$host:$port/app/?audio=0&token=${java.net.URLEncoder.encode(token, "UTF-8")}"

    fun socketUrl(): String =
        "ws://$host:$port/?token=${java.net.URLEncoder.encode(token, "UTF-8")}"

    companion object {
        const val DEFAULT_PORT = 8765
        private const val KEY_HOST = "host"
        private const val KEY_PORT = "port"
        private const val KEY_TOKEN = "token"
        private const val KEY_MIC = "microphone"
    }
}
