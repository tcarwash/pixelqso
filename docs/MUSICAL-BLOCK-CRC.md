# E-major optional block CRC metadata

The transmitter sidecar is disabled to remove its airtime. Set
`ENABLE_MUSICAL_BLOCK_CRC_SIDECAR` in `modem_envelope.py` to `True` to restore it;
receiver support remains available. The current musical transmitter uses native
metadata version 5 with a three-note timing marker every 64 pixel symbols. This adds
45 symbols (1.8 seconds) to a 32×32 color card and lets the receiver re-estimate
symbol timing through the raster. Version-3 unpiloted cards remain supported by
the receiver; older software will not understand version 5.

Version-3 `experimental_8fsk_musical_25` remains unchanged: its native protected
identity header is followed directly by the E-major pixel notes. Version 5
inserts timing markers in the raster and the receiver removes them before
reconstructing the pixels. When enabled, per-block checks travel in optional `PQCH` common-envelope markers
before and after the repeated raster burst. Each marker is independently protected with
the existing RS/CRC header coding.

A 64-byte `PQBC` table contains the protected 16-byte content identity, pixel
count, 64-pixel block size, up to sixteen CRC16 values, reserved zero bytes and
a CRC32 for the table. Four envelope markers carry 16 bytes each before and after the repeated image burst (eight markers per send, regardless of copy count). Their versions are 8 through 11, with their sequence index implied by the version;
the envelope's 32-bit sample field carries the table CRC32. Receivers accept a
table only after all four parts, the token, the table CRC and its structure
validate. Repeated and split tables are retained across receive windows.

An updated receiver checks each complete native pixel block against this table
and can keep matching blocks from different passes. Block flags, covered pixels
and totals are exposed as provisional receive metadata. The existing whole-card
content identity remains the final acceptance check before a card becomes exact.
CRC16 collisions or mixed/corrupt content therefore cannot independently
promote a card.

Older software does not recognize marker versions 8 through 11 and skips those
markers while continuing to decode the original native raster. Existing version
1 copy headers and the pixel-tone sequence do not change. The additional table adds eight short markers per send (four before and four after all repeated raster copies), alongside any animation or reply-report metadata. At the current header rate this is about 12.8 seconds of added airtime per send, regardless of copy count. Images larger than 1024 pixels omit this table.
