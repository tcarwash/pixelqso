# Discovery and provisional reception

Experimental audio modes have distinct framing layers:

- Native preambles/sync train the selected modem's timing and frequency recovery.
  These are waveform patterns, not station metadata, and use the native mode.
- Native card metadata carries layout/palette, callsign, grid, card identity,
  stage, and reported SNR. Its contents depend on the format. Resilient avatars
  protect this metadata separately from their systematic image blocks.
- Common discovery markers use fixed 100-baud 8-FSK with training, Reed–Solomon
  protection, and CRC32. They carry protocol version, mode, payload length, and
  content identity. Ordinary copies have opening and closing markers; grouped
  FSK uses one opening marker with a protected copy count.

Discovery is an acceleration hint. A complete matching hinted decode can return
immediately. Missing, unrelated, failed, identity-mismatched, or partial hinted
decodes permit native acquisition on the original capture. Receive-all retains
that original capture even when cached admitted payloads are materialized, and
continues native mode attempts after a partial hinted result. Grouped FSK can
fall back to its underlying native FSK decoder without the opening marker.
Compressed modes remain disabled and are not included in this change.

Native CRC/content checks still gate verified cards and exchanges. A rejected
discovery identity is not attached to an independently recovered native card.

When a resilient v4 avatar's identity metadata is unavailable, display-only
recovery can show systematic pixels if native sync and a confident supported
layout prefix survive. This currently covers 32×32 resilient 100/50/25 and the
v4 avatar variant. Pixels, layout, and palette in this fallback are tentative;
no callsign, card ID, or verified image regions are claimed. The preview bypasses
card assembly, wall/deck storage, and exchange actions. Later verified native
metadata promotes reception through the ordinary card path. This is not a
general raw-bit preview for LDPC modes: coded/interleaved bits are not directly
image pixels.

New identity selection favors recency rather than recovered pixel count. Native
resilient headers for a later card switch the active raster even with no checked
image blocks yet. Card ID remains part of the assembly key when artwork is
identical. Protected-header callbacks announce the pivot before payload recovery,
and native fallback starts with that newer identity's payload. A synthetic stream
containing an almost finished card followed by a new header and 64 tentative
pixels selected the new card rather than the older image.

Synthetic replay confirmed native full-card recovery despite a discovery hint
for another mode, and a 1024-pixel unidentified preview with deliberately damaged
native identity metadata. Live audio/RF and rendered UI behavior remain
unverified. No container rebuild or deployment was performed.
