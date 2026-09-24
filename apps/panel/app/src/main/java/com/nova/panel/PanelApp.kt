package com.nova.panel

import android.app.Application

/**
 * Nothing to set up at startup that the panel cannot do lazily — but a named
 * Application class is where a crash handler or a log sink would go, and on a
 * device with no visible logs that is worth having a home for.
 */
class PanelApp : Application()
