package com.nova.panel

import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Just enough WAV to play what the core sends back.
 *
 * The core writes canonical files through Python's `wave` module, so "skip 44
 * bytes and play the rest" would work today. It is the kind of shortcut that
 * costs nothing to avoid and, the first time anything writes a `LIST` chunk in
 * front of the audio, turns a reply into a burst of static — a failure that
 * sounds like broken hardware rather than like a parsing bug.
 *
 * So this walks the chunk table, takes the sample rate from `fmt ` rather than
 * assuming it, and tolerates a `data` chunk whose declared length runs past
 * what actually arrived.
 */
object Wav {

    class Clip(val pcm: ByteArray, val sampleRate: Int)

    private const val RIFF = 0x46464952 // "RIFF", little-endian
    private const val WAVE = 0x45564157 // "WAVE"
    private const val FMT = 0x20746d66 // "fmt "
    private const val DATA = 0x61746164 // "data"

    /**
     * @param fallbackRate used when a `data` chunk arrives before `fmt `, which
     *   is legal and rare; the core tells us the rate out of band anyway.
     */
    fun parse(bytes: ByteArray, fallbackRate: Int): Clip? {
        if (bytes.size < 12) return null
        val buffer = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN)
        if (buffer.int != RIFF) return null
        buffer.int // total size, unused — the array's own length is the truth
        if (buffer.int != WAVE) return null

        var sampleRate = fallbackRate
        while (buffer.remaining() >= 8) {
            val id = buffer.int
            val size = buffer.int
            if (size < 0 || size > buffer.remaining()) {
                // Truncated: take what is actually there rather than nothing,
                // so a clipped reply is still heard.
                if (id != DATA) return null
                val pcm = ByteArray(buffer.remaining())
                buffer.get(pcm)
                return Clip(pcm, sampleRate)
            }
            when (id) {
                FMT -> {
                    val start = buffer.position()
                    buffer.short // encoding
                    buffer.short // channels
                    sampleRate = buffer.int
                    buffer.position(start + size)
                }
                DATA -> {
                    val pcm = ByteArray(size)
                    buffer.get(pcm)
                    return Clip(pcm, sampleRate)
                }
                else -> buffer.position(buffer.position() + size)
            }
            // Chunks are word-aligned: an odd length carries a pad byte that is
            // not counted in the size.
            if (size % 2 == 1 && buffer.remaining() > 0) {
                buffer.position(buffer.position() + 1)
            }
        }
        return null
    }
}
