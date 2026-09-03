package com.nova.panel

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Bring the panel back after a power cut.
 *
 * Claiming HOME already means the launcher comes back on its own, but only
 * once something dismisses the boot animation — and a device wired into a wall
 * socket is exactly the one nobody is standing in front of to do that. This
 * makes the panel the first thing on screen, with its microphone already
 * offered to the core.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action
        if (action != Intent.ACTION_BOOT_COMPLETED && action != Intent.ACTION_LOCKED_BOOT_COMPLETED) {
            return
        }
        if (!Prefs(context).configured) return

        context.startActivity(
            Intent(context, PanelActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        )
    }
}
