package com.nova.panel

import android.content.Intent
import android.os.Bundle
import android.view.View
import android.view.inputmethod.EditorInfo
import androidx.appcompat.app.AppCompatActivity
import com.nova.panel.databinding.ActivityPairingBinding

/**
 * Where the panel is told which core it belongs to.
 *
 * Shown once on a fresh install, and reachable afterwards through the corner
 * gesture in `PanelActivity` — which is the only way back in, since the panel
 * has no navigation bar and owns the home button.
 *
 * The token is the same one the desktop shell is handed over IPC and the web
 * client takes from its pairing link. There is no discovery here on purpose:
 * guessing at hosts on a network is how a panel ends up talking to something
 * that merely answers.
 */
class PairingActivity : AppCompatActivity() {

    private lateinit var binding: ActivityPairingBinding
    private lateinit var prefs: Prefs

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        binding = ActivityPairingBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.host.setText(prefs.host)
        binding.port.setText(prefs.port.toString())
        binding.token.setText(prefs.token)
        binding.microphone.isChecked = prefs.microphone

        binding.save.setOnClickListener { save() }

        // Done on the token field submits the form. Without this, the key most
        // obviously offered at the end of the last field does nothing, which
        // reads as the app ignoring you — especially on a short screen where
        // the button is the less discoverable of the two.
        binding.token.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_DONE) {
                save()
                true
            } else {
                false
            }
        }
    }

    private fun save() {
        val host = binding.host.text.toString().trim()
        val token = binding.token.text.toString().trim()
        val port = binding.port.text.toString().trim().toIntOrNull() ?: Prefs.DEFAULT_PORT

        // Inline rather than a Toast: a panel is often set up with
        // notifications never granted, and a Toast that never appears turns a
        // refused form into a button that seems to do nothing at all.
        if (host.isEmpty()) return fail(getString(R.string.pair_need_host), binding.host)
        if (token.isEmpty()) return fail(getString(R.string.pair_need_token), binding.token)

        prefs.host = host
        prefs.port = port
        prefs.token = token
        prefs.microphone = binding.microphone.isChecked

        // The service holds the old address in a live socket, so it has to be
        // torn down rather than left to reconnect somewhere that has moved.
        AudioService.stop(this)

        startActivity(
            Intent(this, PanelActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        )
        finish()
    }

    private fun fail(message: String, field: View) {
        binding.status.text = message
        binding.status.visibility = View.VISIBLE
        field.requestFocus()
    }
}
