package com.nova.panel

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.view.KeyEvent
import android.view.View
import android.view.WindowManager
import android.webkit.ConsoleMessage
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import com.nova.panel.databinding.ActivityPanelBinding

/**
 * The whole device, as far as anyone standing in front of it is concerned.
 *
 * A WebView holding the same interface the desktop shell runs — same Core
 * animation, same surfaces, same settings — pointed at the core over the
 * network. What this class adds is everything a browser tab does not do: it
 * never leaves, it never sleeps, it never shows a URL bar, and it comes back
 * by itself after a reboot or a crash.
 *
 * It deliberately does not touch audio. That lives in `AudioService`, where it
 * survives the screen going off and does not depend on a WebView being handed
 * a microphone it cannot have over plain HTTP.
 */
class PanelActivity : AppCompatActivity() {

    private lateinit var binding: ActivityPanelBinding
    private lateinit var prefs: Prefs
    private var loaded = false

    private val requestMicrophone =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            // A refusal is survivable: the panel is still a display, and the
            // settings screen can still be reached to change its mind.
            if (granted && prefs.microphone) AudioService.start(this)
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)

        if (!prefs.configured) {
            startActivity(Intent(this, PairingActivity::class.java))
            finish()
            return
        }

        binding = ActivityPanelBinding.inflate(layoutInflater)
        setContentView(binding.root)

        goImmersive()
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        }

        configureWebView()
        binding.web.loadUrl(prefs.appUrl())

        // Back would leave a blank WebView behind on a device with no other
        // screen to go to, so it does nothing at all here.
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() = Unit
        })

        wireEscapeHatch()
        ensureAudio()
    }

    /**
     * Five taps in the corner, within a couple of seconds, opens the panel's
     * own settings.
     *
     * Not a long press: the WebView swallows those, and a long press is
     * something a cloth wiping the screen can produce. A deliberate count in a
     * deliberate place is hard to do by accident and easy to remember.
     */
    private fun wireEscapeHatch() {
        var taps = 0
        var firstTapAt = 0L
        binding.escapeHatch.setOnClickListener {
            val now = System.currentTimeMillis()
            if (now - firstTapAt > ESCAPE_WINDOW_MS) {
                taps = 0
                firstTapAt = now
            }
            if (++taps >= ESCAPE_TAPS) {
                taps = 0
                startActivity(Intent(this, PairingActivity::class.java))
            }
        }
    }

    override fun onResume() {
        super.onResume()
        goImmersive()
        // Coming back from the settings screen, the address or token may have
        // changed underneath the page that is loaded.
        if (loaded && binding.web.url?.startsWith("http://${prefs.host}:${prefs.port}") == false) {
            binding.web.loadUrl(prefs.appUrl())
        }
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        // A system dialog or a notification shade pulls the bars back; take
        // them away again as soon as focus returns.
        if (hasFocus) goImmersive()
    }

    /**
     * Swallow the keys that would navigate away, and only those.
     *
     * An earlier version swallowed everything, which locked the panel down
     * beautifully and made it impossible to type into N.O.V.A.'s own settings
     * — the text fields are inside the WebView, and a Bluetooth keyboard is
     * how anyone would actually fill them in.
     */
    override fun dispatchKeyEvent(event: KeyEvent): Boolean = when (event.keyCode) {
        KeyEvent.KEYCODE_HOME,
        KeyEvent.KEYCODE_MENU,
        KeyEvent.KEYCODE_APP_SWITCH,
        KeyEvent.KEYCODE_SEARCH -> true
        else -> super.dispatchKeyEvent(event)
    }

    // ---------------------------------------------------------------- webview

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebView() = with(binding.web) {
        settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true // the app keeps its bridge token here
            // Everything the page loads comes from the core; nothing is
            // fetched from the internet, so the cache is only ever our own.
            cacheMode = WebSettings.LOAD_DEFAULT
            mediaPlaybackRequiresUserGesture = false
            useWideViewPort = true
            loadWithOverviewMode = false
            setSupportZoom(false)
            builtInZoomControls = false
            textZoom = 100 // a panel is read at arm's length, not held
        }
        setBackgroundColor(Color.parseColor("#04060D"))
        overScrollMode = View.OVER_SCROLL_NEVER
        isVerticalScrollBarEnabled = false
        isHorizontalScrollBarEnabled = false
        isLongClickable = false
        setOnLongClickListener { true } // no text selection callout on a wall panel

        webChromeClient = object : WebChromeClient() {
            /**
             * Granted so the page's own microphone works if it is ever asked
             * for — a local build over HTTPS, say. In the normal case the page
             * is loaded with `audio=0` and never asks, because capture belongs
             * to AudioService.
             */
            override fun onPermissionRequest(request: PermissionRequest) {
                runOnUiThread { request.grant(request.resources) }
            }

            override fun onConsoleMessage(message: ConsoleMessage): Boolean {
                if (message.messageLevel() == ConsoleMessage.MessageLevel.ERROR) {
                    android.util.Log.w(TAG, "page: ${message.message()}")
                }
                return true
            }
        }

        webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                view: WebView,
                request: WebResourceRequest,
            ): Boolean {
                // The interface is a single page. Anything trying to navigate
                // elsewhere is a mistake or a link, and neither belongs on a
                // device with no way back.
                val target = request.url
                return !(target.host == prefs.host && target.port.let { it == prefs.port || it == -1 })
            }

            override fun onPageFinished(view: WebView, url: String) {
                loaded = true
                binding.status.visibility = View.GONE
            }

            override fun onReceivedError(
                view: WebView,
                request: WebResourceRequest,
                error: WebResourceError,
            ) {
                if (!request.isForMainFrame) return
                // The core may simply not be up yet — it is a service on
                // another machine that reboots too. Say so and keep trying,
                // rather than leaving a white error page on the wall.
                showStatus(getString(R.string.status_offline, prefs.host))
                view.postDelayed({ if (!isFinishing) view.loadUrl(prefs.appUrl()) }, RETRY_MS)
            }
        }
    }

    private fun showStatus(text: String) {
        binding.status.text = text
        binding.status.visibility = View.VISIBLE
    }

    // ------------------------------------------------------------------ audio

    private fun ensureAudio() {
        if (!prefs.microphone) {
            AudioService.stop(this)
            return
        }
        val granted = ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        if (granted) AudioService.start(this) else requestMicrophone.launch(Manifest.permission.RECORD_AUDIO)
    }

    // ------------------------------------------------------------- fullscreen

    private fun goImmersive() {
        WindowCompat.setDecorFitsSystemWindows(window, false)
        WindowInsetsControllerCompat(window, binding.root).apply {
            hide(WindowInsetsCompat.Type.systemBars())
            systemBarsBehavior =
                WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        }
    }

    companion object {
        private const val TAG = "NovaPanel"
        private const val RETRY_MS = 4000L
        private const val ESCAPE_TAPS = 5
        private const val ESCAPE_WINDOW_MS = 2500L
    }
}
