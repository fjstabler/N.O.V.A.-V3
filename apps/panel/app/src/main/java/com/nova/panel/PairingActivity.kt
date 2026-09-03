package com.nova.panel

import android.content.Intent
import android.os.Bundle
import android.widget.Toast
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
    }

    private fun save() {
        val host = binding.host.text.toString().trim()
        val token = binding.token.text.toString().trim()
        val port = binding.port.text.toString().trim().toIntOrNull() ?: Prefs.DEFAULT_PORT

        if (host.isEmpty() || token.isEmpty()) {
            Toast.makeText(this, R.string.pair_incomplete, Toast.LENGTH_SHORT).show()
            return
        }

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
}
