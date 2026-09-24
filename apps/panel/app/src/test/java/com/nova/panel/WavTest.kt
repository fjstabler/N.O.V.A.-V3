package com.nova.panel

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.Base64

/**
 * The parser between the core's reply and the speaker.
 *
 * Worth testing rather than eyeballing because every way of getting it wrong
 * sounds the same from across a room: static, or silence. A misread offset
 * does not throw, it just plays the header as audio.
 */
class WavTest {

    private fun chunk(id: String, body: ByteArray): ByteArray {
        val out = ByteArrayOutputStream()
        out.write(id.toByteArray(Charsets.US_ASCII))
        out.write(le32(body.size))
        out.write(body)
        if (body.size % 2 == 1) out.write(0) // word alignment pad
        return out.toByteArray()
    }

    private fun le32(value: Int) =
        ByteBuffer.allocate(4).order(ByteOrder.LITTLE_ENDIAN).putInt(value).array()

    private fun le16(value: Int) =
        ByteBuffer.allocate(2).order(ByteOrder.LITTLE_ENDIAN).putShort(value.toShort()).array()

    private fun fmt(rate: Int): ByteArray {
        val out = ByteArrayOutputStream()
        out.write(le16(1))      // PCM
        out.write(le16(1))      // mono
        out.write(le32(rate))
        out.write(le32(rate * 2)) // byte rate
        out.write(le16(2))      // block align
        out.write(le16(16))     // bits per sample
        return chunk("fmt ", out.toByteArray())
    }

    private fun wav(vararg chunks: ByteArray): ByteArray {
        val body = ByteArrayOutputStream()
        body.write("WAVE".toByteArray(Charsets.US_ASCII))
        chunks.forEach { body.write(it) }
        val out = ByteArrayOutputStream()
        out.write("RIFF".toByteArray(Charsets.US_ASCII))
        out.write(le32(body.size()))
        out.write(body.toByteArray())
        return out.toByteArray()
    }

    private val audio = byteArrayOf(1, 0, 2, 0, 3, 0, 4, 0)

    @Test
    fun `reads the audio and the rate from an ordinary file`() {
        val clip = Wav.parse(wav(fmt(24000), chunk("data", audio)), 16000)!!

        assertArrayEquals(audio, clip.pcm)
        // From the file, not from the fallback — Kokoro's 24 kHz played as
        // 16 kHz is the deep, slow voice that sounds like a flat battery.
        assertEquals(24000, clip.sampleRate)
    }

    @Test
    fun `skips a chunk it does not care about`() {
        // The case the whole parser exists for: anything before `data` shifts
        // the audio, and a fixed 44-byte skip would play the metadata.
        val list = chunk("LIST", "INFOISFTNova".toByteArray(Charsets.US_ASCII))
        val clip = Wav.parse(wav(fmt(16000), list, chunk("data", audio)), 16000)!!

        assertArrayEquals(audio, clip.pcm)
    }

    @Test
    fun `handles an odd-length chunk and its pad byte`() {
        val odd = chunk("LIST", byteArrayOf(9, 9, 9))
        val clip = Wav.parse(wav(fmt(16000), odd, chunk("data", audio)), 16000)!!

        assertArrayEquals(audio, clip.pcm)
    }

    @Test
    fun `plays what arrived when the data chunk is truncated`() {
        // A reply cut short by a dropped connection should still be heard up to
        // the point it stopped, rather than discarded whole.
        val full = wav(fmt(16000), chunk("data", audio))
        val clip = Wav.parse(full.copyOf(full.size - 4), 16000)!!

        assertEquals(4, clip.pcm.size)
        assertArrayEquals(audio.copyOf(4), clip.pcm)
    }

    @Test
    fun `falls back to the rate the core reported when fmt comes late`() {
        val clip = Wav.parse(wav(chunk("data", audio)), 22050)!!
        assertEquals(22050, clip.sampleRate)
    }

    @Test
    fun `refuses anything that is not a WAV`() {
        assertNull(Wav.parse(ByteArray(0), 16000))
        assertNull(Wav.parse("not audio at all".toByteArray(Charsets.US_ASCII), 16000))
        assertNull(Wav.parse(wav(fmt(16000)), 16000)) // header, no audio
    }

    /**
     * The real thing, not one this test built.
     *
     * Both halves of a format agreeing with themselves is the classic way a
     * cross-language pair passes its tests and fails on the wire, so this is a
     * clip produced by the core's own `samples_to_wav_base64` — 240 float
     * samples at 24 kHz, which is what Kokoro hands the speaker.
     */
    @Test
    fun `reads a clip the core actually produced`() {
        val bytes = Base64.getDecoder().decode(CORE_CLIP)

        val clip = Wav.parse(bytes, 16000)!!

        assertEquals(24000, clip.sampleRate)
        assertEquals(240 * 2, clip.pcm.size) // 16-bit mono
        // Starts at silence and rises: a header read as audio would not.
        assertEquals(0, clip.pcm[0].toInt())
        assertEquals(0, clip.pcm[1].toInt())
    }

    @Test
    fun `refuses a file whose RIFF header is right but whose type is not WAVE`() {
        val out = ByteArrayOutputStream()
        out.write("RIFF".toByteArray(Charsets.US_ASCII))
        out.write(le32(4))
        out.write("AVI ".toByteArray(Charsets.US_ASCII))

        assertNull(Wav.parse(out.toByteArray(), 16000))
    }

    companion object {
        private const val CORE_CLIP =
            "UklGRgQCAABXQVZFZm10IBAAAAABAAEAwF0AAIC7AAACABAAZGF0YeABAAAAAFkFqQrlDwYVABrMHmEjtifFK4Uv7zL/Na049jrWPEg+Sz/dP/w/qD/jPq09CDz3OX83pDRqMdct8ynDJU8hoBy+F7ESgg08COYCjf03+O7yve2t6MfjE9+b2mXWetLgzp7Lu8g6xiHEc8IzwWTAB8AcwKTAncEHw93EHcfEycvML9Do0+/XP9zP4Jfljuqr7+b0NPqN/+YENwp2D5kUlxlnHgEjXCdwKzcvqTLANXc4yTqyPC4+Oj/VP/4/tD/4Pss9LzwoOrg35TSzMScuSSogJrIhBx0pGB8T8w2uCFkDAP6p+F/zLO4Z6S/kdt/42r3Wy9Iqz+HL9chsxkrEk8JKwXHACsAWwJTAhMHkwrHE6caHyYfM4s+U05bX4Ntr4C7lIeo873T0wvka/3MExQkGDysULRkBHqAiAScbK+kuYjKCNUE4mzqNPBI+KD/MP/8/vj8MP+g9VjxYOvE3JTX7MXcuoCp8JhMibh2TGI0TYw4gCc0Dc/4c+dDzm+6F6Zfk2t9X2xXXHdN1zyTMMMmexnPEs8JhwX/ADsAQwIXAbMHCwobEtcZLyULMl89C0z3XgdsG4MXktenN7gP0T/mn/gAEUwmWDr4TwxibHT8ipSbGKpouGzJCNQo4bTo="
    }
}
